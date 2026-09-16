import importlib.util
import json
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "service/connection_transfer.py"
spec = importlib.util.spec_from_file_location("connection_transfer", SOURCE)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class ConnectionTransferTests(unittest.TestCase):
    def setUp(self):
        self.s3 = {"id": "local-id", "name": "Family Photos", "backend": "s3",
                   "bucket": "family-photos", "profile": "personal", "region": "us-east-1",
                   "readOnly": False, "autoConnect": True,
                   "cacheMaxSizeMiB": 512, "cacheMaxAgeHours": 6}
        self.sftp = {"name": "Server files", "backend": "sftp", "host": "files.example.com",
                     "user": "photographer", "port": 2222, "remotePath": '/Family "photos":ro',
                     "authMode": "keyFile", "readOnly": True, "autoConnect": True,
                     "cacheMaxSizeMiB": 2048, "cacheMaxAgeHours": 24}

    def document(self, source=None, **overrides):
        result = {"format": transfer.FORMAT, "version": transfer.VERSION,
                  "connection": transfer.decode(transfer.encode(source or self.s3))}
        result.update(overrides)
        return result

    def decode_document(self, document):
        return transfer.decode(json.dumps(document).encode())

    def test_s3_round_trip_keeps_destination_and_settings(self):
        result = transfer.decode(transfer.encode(self.s3))
        self.assertEqual(result, {key: value for key, value in dict(self.s3, autoConnect=False).items() if key != "id"})
        self.assertTrue(self.s3["autoConnect"])  # Export does not mutate the saved connection.

    def test_sftp_round_trip_keeps_destination_and_authentication_choice(self):
        for mode in ("agent", "keyFile", "password"):
            with self.subTest(mode=mode):
                source = dict(self.sftp, authMode=mode)
                self.assertEqual(transfer.decode(transfer.encode(source)), dict(source, autoConnect=False))

    def test_nonportable_fields_and_credentials_are_never_serialized(self):
        secrets = {
            "password": "unique-password-secret", "privateKey": "unique-private-key-content",
            "accessKeyId": "unique-aws-access-key", "secretAccessKey": "unique-aws-secret-key",
            "sessionToken": "unique-session-token", "awsCredentials": {"secret": "nested-secret"},
            "keyFile": "/Users/other/.ssh/private", "knownHostsFile": "/Users/other/.ssh/known_hosts",
            "mountPoint": "/Volumes/Local Drive", "cachePath": "/Users/other/cache",
            "id": "unique-local-identity", "runtime": {"pid": 42}, "desiredConnected": True,
            "passwordConfigured": True, "revision": 17, "updatedAt": 1234,
        }
        for source in (self.s3, self.sftp):
            with self.subTest(backend=source["backend"]):
                data = transfer.encode(dict(source, **secrets))
                exported = json.loads(data)["connection"]
                self.assertFalse(set(exported) & set(secrets))
                for value in secrets.values():
                    if isinstance(value, str):
                        self.assertNotIn(value.encode(), data)
                other_backend = "sftp" if source["backend"] == "s3" else "s3"
                self.assertFalse(set(exported) & transfer.BACKEND_FIELDS[other_backend])

    def test_legacy_s3_records_receive_portable_defaults(self):
        legacy = {key: value for key, value in self.s3.items()
                  if key not in ("backend", "cacheMaxSizeMiB", "cacheMaxAgeHours")}
        result = transfer.decode(transfer.encode(legacy))
        self.assertEqual(result["backend"], "s3")
        self.assertEqual(result["cacheMaxSizeMiB"], 2048)
        self.assertEqual(result["cacheMaxAgeHours"], 24)

    def test_import_never_enables_automatic_connection(self):
        document = self.document()
        document["connection"]["autoConnect"] = True
        self.assertFalse(self.decode_document(document)["autoConnect"])

    def test_invalid_json_and_encoding_are_safe_errors(self):
        for data in (b"", b"not json", b"\xff", b'{"privateKey":"supersecret",}', b"[]",
                     b"null", b"{" * 2000, b"[" * 2000 + b"]" * 2000):
            with self.subTest(data=data[:30]), self.assertRaises(transfer.ConnectionTransferError) as raised:
                transfer.decode(data)
            self.assertNotIn("supersecret", str(raised.exception))

    def test_size_limit_is_enforced_before_json_parsing(self):
        with self.assertRaisesRegex(transfer.ConnectionTransferError, "64 KiB"):
            transfer.decode(b" " * (transfer.MAX_FILE_BYTES + 1))

    def test_duplicate_fields_and_nonfinite_numbers_are_rejected(self):
        good = transfer.encode(self.s3).decode()
        invalid = [good.replace('"version": 1', '"version": 1, "version": 1'),
                   good.replace('"readOnly": false', '"readOnly": false, "readOnly": true')]
        invalid += [good.replace('"version": 1', '"version": ' + value)
                    for value in ("NaN", "Infinity", "-Infinity")]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(transfer.ConnectionTransferError):
                transfer.decode(data.encode())

    def test_document_format_version_and_fields_are_strict(self):
        for changes in ({"format": "other.app"}, {"version": True}, {"version": 1.0},
                        {"version": "1"}, {"version": 2}, {"version": None},
                        {"credentials": "private"}, {"connection": []}):
            with self.subTest(changes=changes), self.assertRaises(transfer.ConnectionTransferError):
                self.decode_document(self.document(**changes))
        document = self.document()
        del document["format"]
        with self.assertRaises(transfer.ConnectionTransferError):
            self.decode_document(document)

    def test_injected_sensitive_and_cross_backend_fields_are_rejected(self):
        for key, value in (("password", "supersecret"), ("id", "foreign-id"),
                           ("keyFile", "/tmp/key"), ("host", "other.example.com"),
                           ("futureField", True)):
            document = self.document()
            document["connection"][key] = value
            with self.subTest(key=key), self.assertRaises(transfer.ConnectionTransferError) as raised:
                self.decode_document(document)
            self.assertNotIn("supersecret", str(raised.exception))

    def test_required_fields_cannot_be_omitted(self):
        for source in (self.s3, self.sftp):
            for key in self.document(source)["connection"]:
                document = self.document(source)
                del document["connection"][key]
                with self.subTest(backend=source["backend"], key=key), self.assertRaises(transfer.ConnectionTransferError):
                    self.decode_document(document)

    def test_field_types_and_numeric_bounds_are_strict(self):
        invalid = [("backend", []), ("backend", "webdav"), ("name", None), ("bucket", 123),
                   ("readOnly", 1), ("autoConnect", "false"), ("cacheMaxSizeMiB", True),
                   ("cacheMaxSizeMiB", 63), ("cacheMaxSizeMiB", 1048577), ("cacheMaxAgeHours", 1.5),
                   ("cacheMaxAgeHours", 0), ("cacheMaxAgeHours", 8761)]
        for key, value in invalid:
            document = self.document()
            document["connection"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(transfer.ConnectionTransferError):
                self.decode_document(document)

    def test_names_and_s3_fields_reject_unsafe_values(self):
        invalid = [("name", ""), ("name", " .hidden"), ("name", "../escape"),
                   ("name", "Bad:Name"), ("name", "N" * 181), ("name", "é" * 91),
                   ("name", "\ud800"), ("name", "bad\x7f"), ("bucket", "s3://bucket"),
                   ("profile", "other\nsecret=x"), ("region", "us-east-1\n[remote]")]
        for key, value in invalid:
            document = self.document()
            document["connection"][key] = value
            with self.subTest(key=key, value=repr(value)), self.assertRaises(transfer.ConnectionTransferError):
                self.decode_document(document)

    def test_sftp_fields_reject_config_injection_and_invalid_paths(self):
        invalid = [("host", "sftp://example.com"), ("host", "host:22"), ("host", ""),
                   ("host", "server\npass=secret"), ("host", "-invalid.example.com"),
                   ("user", "a b"), ("user", "user\npass=secret"), ("port", 0),
                   ("port", 65536), ("port", True), ("port", "22"),
                   ("remotePath", "../other"), ("remotePath", "/files/../other"),
                   ("remotePath", "~/photos"), ("remotePath", "a\n[remote]"),
                   ("remotePath", "/" + "x" * 4096), ("authMode", "unknown")]
        for key, value in invalid:
            document = self.document(self.sftp)
            document["connection"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(transfer.ConnectionTransferError):
                self.decode_document(document)

    def test_sftp_accepts_ipv6_and_normalizes_names_without_local_files(self):
        source = dict(self.sftp, host=" 2001:db8::1 ", user=" photographer ", name=" Server files ")
        result = transfer.decode(transfer.encode(source))
        self.assertEqual(result["host"], "2001:db8::1")
        self.assertEqual(result["user"], "photographer")
        self.assertEqual(result["name"], "Server files")
        self.assertNotIn("knownHostsFile", result)
        self.assertNotIn("keyFile", result)

    def test_utf8_names_round_trip(self):
        self.assertEqual(transfer.decode(transfer.encode(dict(self.s3, name="Café Photos 🐢")))["name"], "Café Photos 🐢")


if __name__ == "__main__":
    unittest.main()
