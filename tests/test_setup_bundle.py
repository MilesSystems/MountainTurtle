import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import setup_bundle as bundle


class SetupBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="turtle-setup-tests-")
        cls.keys = {}
        for algorithm, bits in (("ed25519", None), ("rsa", "2048"), ("ecdsa", "256")):
            path = Path(cls.directory.name) / algorithm
            command = [bundle.SSH_KEYGEN, "-q", "-t", algorithm, "-N", "", "-f", str(path)]
            if bits:
                command.extend(("-b", bits))
            subprocess.run(command, check=True, capture_output=True)
            cls.keys[algorithm] = (path.read_bytes(), path.with_suffix(".pub").read_text().split()[:2])
        encrypted = Path(cls.directory.name) / "encrypted"
        subprocess.run([bundle.SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "synthetic test passphrase",
                        "-f", str(encrypted)], check=True, capture_output=True)
        cls.encrypted = encrypted.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.connection = {"name": "Family files", "backend": "sftp", "host": "files.example.com",
                           "user": "family", "port": 2222, "remotePath": "/", "authMode": "keyFile",
                           "readOnly": False, "autoConnect": True, "cacheMaxSizeMiB": 2048,
                           "cacheMaxAgeHours": 24, "keyFile": "/local/key", "id": "local-id"}
        self.key = self.keys["ed25519"][0]
        self.hosts = self.pin()

    def pin(self, host="[files.example.com]:2222", algorithm="ed25519"):
        kind, value = self.keys[algorithm][1]
        return f"{host} {kind} {value}\n".encode()

    def document(self):
        return json.loads(bundle.encode(self.connection, self.key, self.hosts))

    def decode_document(self, document):
        return bundle.decode(json.dumps(document).encode())

    def test_complete_roundtrip_excludes_local_paths_and_keeps_disconnect_default(self):
        data = bundle.encode(self.connection, self.key, self.hosts)
        imported = bundle.decode(data)
        self.assertTrue(bundle.is_setup(data))
        self.assertEqual(imported["privateKey"], self.key)
        self.assertEqual(imported["knownHosts"], self.hosts)
        self.assertEqual(imported["connection"]["host"], self.connection["host"])
        self.assertEqual(imported["connection"]["remotePath"], "/")
        self.assertFalse(imported["connection"]["autoConnect"])
        self.assertNotIn(b"/local/key", data)
        self.assertNotIn(b"local-id", data)
        self.assertTrue(self.connection["autoConnect"])

    def test_rsa_and_ecdsa_credentials_and_hostkeys_work(self):
        for algorithm in ("rsa", "ecdsa"):
            with self.subTest(algorithm=algorithm):
                data = bundle.encode(self.connection, self.keys[algorithm][0], self.pin(algorithm=algorithm))
                self.assertEqual(bundle.decode(data)["privateKey"], self.keys[algorithm][0])

    def test_only_matching_endpoint_is_exported(self):
        source = self.pin(host="unrelated.example.com") + self.hosts + self.pin(host="files.example.com")
        result = bundle.decode(bundle.encode(self.connection, self.key, source))
        self.assertEqual(result["knownHosts"], self.hosts)
        self.assertNotIn(b"unrelated", result["knownHosts"])

    def test_large_existing_trust_store_is_filtered_before_bundle_limit(self):
        source = self.pin(host="unrelated.example.com") * 300 + self.hosts
        self.assertGreater(len(source), bundle.MAX_KNOWN_HOSTS_BYTES)
        result = bundle.decode(bundle.encode(self.connection, self.key, source))
        self.assertEqual(result["knownHosts"], self.hosts)

    def test_hashed_known_hosts_are_matched_and_normalized(self):
        path = Path(self.directory.name) / "hashed_hosts"
        path.write_bytes(self.hosts + self.pin(host="unrelated.example.com"))
        subprocess.run([bundle.SSH_KEYGEN, "-H", "-f", str(path)], check=True, capture_output=True)
        source = path.read_bytes()
        self.assertNotIn(b"files.example.com", source)
        imported = bundle.decode(bundle.encode(self.connection, self.key, source))
        self.assertEqual(imported["knownHosts"], self.hosts)
        hashed_only = source.splitlines(keepends=True)[0]
        document = self.document()
        document["knownHosts"] = base64.b64encode(hashed_only).decode()
        self.assertEqual(self.decode_document(document)["knownHosts"], self.hosts)

    def test_default_port_and_ipv6_use_correct_endpoint_matching(self):
        for host, port, pin_host in (("files.example.com", 22, "files.example.com"),
                                     ("2001:db8::1", 22, "2001:db8::1"),
                                     ("2001:db8::1", 2222, "[2001:db8::1]:2222")):
            with self.subTest(host=host, port=port):
                result = bundle.decode(bundle.encode(dict(self.connection, host=host, port=port),
                                                       self.key, self.pin(host=pin_host)))
                self.assertEqual(result["knownHosts"], self.pin(host=pin_host))

    def test_extra_known_hosts_entries_cannot_be_imported(self):
        document = self.document()
        for source in (self.hosts + self.pin(host="unrelated.example.com"),
                       self.hosts + b"Host *\n  StrictHostKeyChecking no\n",
                       self.pin(host="files.example.com"), self.pin(host="[files.example.com]:2223")):
            with self.subTest(source=source[:40]):
                document["knownHosts"] = base64.b64encode(source).decode()
                with self.assertRaises(bundle.SetupBundleError):
                    self.decode_document(document)

    def test_host_patterns_markers_and_aliases_are_rejected(self):
        for host in ("*", "[*.example.com]:2222", "[files.example.com]:2222,other.example.com",
                     "@cert-authority [files.example.com]:2222", "@revoked [files.example.com]:2222",
                     "!other.example.com,[files.example.com]:2222"):
            with self.subTest(host=host), self.assertRaises(bundle.SetupBundleError):
                bundle.encode(self.connection, self.key, self.pin(host=host))

    def test_validated_hostkey_blobs_must_match_their_advertised_type(self):
        kind, encoded = self.keys["ed25519"][1]
        malformed = [b"[files.example.com]:2222 ssh-ed25519 invalid\n",
                     self.hosts.replace(b"ssh-ed25519", b"ssh-rsa"),
                     self.hosts.replace(b"ssh-ed25519", b"ssh-dss"),
                     f"[files.example.com]:2222 {kind} {base64.b64encode(b'garbage').decode()}\n".encode(),
                     f"[files.example.com]:2222 {kind} {base64.b64encode(base64.b64decode(encoded) + b'garbage').decode()}\n".encode()]
        for hosts in malformed:
            with self.subTest(hosts=hosts[:60]), self.assertRaises(bundle.SetupBundleError):
                bundle.encode(self.connection, self.key, hosts)

    def test_comments_and_duplicate_pins_do_not_become_configuration(self):
        source = b"# personal comment\n" + self.hosts.rstrip() + b" owner comment\n" + self.hosts
        imported = bundle.decode(bundle.encode(self.connection, self.key, source))
        self.assertEqual(imported["knownHosts"], self.hosts)

    def test_private_key_must_be_one_complete_unencrypted_valid_key(self):
        for key in (b"", b"not-a-key SECRET-SENTINEL", self.encrypted, self.key + self.key,
                    self.key + b"Host *\n", b" " + self.key,
                    self.key.replace(b"OPENSSH PRIVATE KEY", b"PUBLIC KEY"),
                    self.key[:70] + b"!" + self.key[71:]):
            with self.subTest(key_size=len(key)), self.assertRaises(bundle.SetupBundleError) as raised:
                bundle.encode(self.connection, key, self.hosts)
            self.assertNotIn("SECRET-SENTINEL", str(raised.exception))

    def test_setup_only_accepts_sftp_keyfile_authentication(self):
        for mode in ("password", "agent"):
            with self.subTest(mode=mode), self.assertRaises(bundle.SetupBundleError):
                bundle.encode(dict(self.connection, authMode=mode), self.key, self.hosts)
        s3 = {"name": "S3", "backend": "s3", "bucket": "test-bucket", "profile": "default",
              "region": "us-east-1"}
        with self.assertRaises(bundle.SetupBundleError):
            bundle.encode(s3, self.key, self.hosts)

    def test_version_types_and_unknown_settings_are_rejected(self):
        for key, value in (("version", True), ("version", 2), ("version", 1.0),
                           ("format", "other.app"), ("sshConfig", "Host *"),
                           ("connection", []), ("privateKey", {}), ("knownHosts", None)):
            document = self.document()
            document[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(bundle.SetupBundleError):
                self.decode_document(document)
        for key in self.document():
            document = self.document()
            del document[key]
            with self.subTest(missing=key), self.assertRaises(bundle.SetupBundleError):
                self.decode_document(document)

    def test_connection_allowlist_is_strict_on_import(self):
        for key, value in (("keyFile", "../../other-key"), ("knownHostsFile", "/tmp/trust"),
                           ("password", "SECRET-SENTINEL"), ("port", True), ("host", "server\noption=value")):
            document = self.document()
            document["connection"][key] = value
            with self.subTest(key=key), self.assertRaises(bundle.SetupBundleError) as raised:
                self.decode_document(document)
            self.assertNotIn("SECRET-SENTINEL", str(raised.exception))

    def test_noncanonical_or_invalid_base64_is_rejected(self):
        document = self.document()
        for field in ("privateKey", "knownHosts"):
            for value in ("!", "c2VjcmV0\n", "", "å", document[field] + "=", "YQ==="):
                modified = dict(document, **{field: value})
                with self.subTest(field=field, value=value[:30]), self.assertRaises(bundle.SetupBundleError):
                    self.decode_document(modified)

    def test_parser_rejects_duplicates_invalid_encoding_and_deep_or_oversized_json(self):
        data = bundle.encode(self.connection, self.key, self.hosts)
        invalid = [data.replace(b'"version": 1', b'"version": 1, "version": 1'),
                   data.replace(b'"version": 1', b'"version": NaN'),
                   b"\xff", b"not json", b"null", b"[]", b"[" * 2000 + b"]" * 2000,
                   b" " * (bundle.MAX_FILE_BYTES + 1)]
        for value in invalid:
            with self.subTest(value=value[:30]), self.assertRaises(bundle.SetupBundleError):
                bundle.decode(value)

    def test_individual_credential_size_limits_are_enforced(self):
        with self.assertRaises(bundle.SetupBundleError):
            bundle.encode(self.connection, b"a" * (bundle.MAX_PRIVATE_KEY_BYTES + 1), self.hosts)
        with self.assertRaises(bundle.SetupBundleError):
            bundle.encode(self.connection, self.key, b"a" * (bundle.MAX_KNOWN_HOSTS_SOURCE_BYTES + 1))
        document = self.document()
        document["knownHosts"] = base64.b64encode(b"a" * (bundle.MAX_KNOWN_HOSTS_BYTES + 1)).decode()
        with self.assertRaises(bundle.SetupBundleError):
            self.decode_document(document)

    def test_format_identification_does_not_claim_full_validation(self):
        self.assertTrue(bundle.is_setup(json.dumps({"format": bundle.FORMAT, "version": 900}).encode()))
        for data in (b"", b"[]", b'{}', b'{"format":"io.mountainturtle.connection"}', b"[" * 2000):
            self.assertFalse(bundle.is_setup(data))

    def test_ssh_keygen_failure_is_generic_and_does_not_expose_output(self):
        with patch.object(bundle.subprocess, "run", side_effect=subprocess.TimeoutExpired("SECRET-SENTINEL", 10)):
            with self.assertRaises(bundle.SetupBundleError) as raised:
                bundle.encode(self.connection, self.key, self.hosts)
        self.assertNotIn("SECRET-SENTINEL", str(raised.exception))

    def test_validation_temporary_credentials_are_private_and_removed(self):
        real_run = bundle.subprocess.run
        paths = []

        def inspect_run(arguments, **kwargs):
            path = Path(arguments[arguments.index("-f") + 1])
            paths.append(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            return real_run(arguments, **kwargs)

        with patch.object(bundle.subprocess, "run", side_effect=inspect_run):
            bundle.encode(self.connection, self.key, self.hosts)
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))


if __name__ == "__main__":
    unittest.main()
