"""Exercise the actual native settings transaction with an in-memory service.

No app is launched and no service, store, mount, or credential is accessed. The
app's updateDrive method and response models are compiled directly from source;
only its service transport, refresh side effects, and polling clock are replaced.
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import Foundation
import SwiftUI

let moss = Color.green

__RESPONSE_MODELS__

// Poll instantly so a timeout exercises the real bounded loop without a delay.
enum Task {
    static func sleep(nanoseconds: UInt64) async throws {}
}

enum ServiceClient {
    static var scenario = ""
    static var calls: [[String]] = []
    static var disconnectPolls = 0
    static var disconnectRequested = false
    static var unsafeAccess = false
    static var connection = Connection(id: "fixture", name: "Test drive", bucket: "fixture-bucket",
        profile: "fixture", region: "us-east-1", readOnly: false, autoConnect: false,
        desiredConnected: false, state: "disconnected", mountPath: "/fixture/Test drive", mounted: false)

    static func data(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    static func run(_ arguments: [String]) async throws -> Data {
        calls.append(arguments)
        switch arguments.first {
        case "status":
            if disconnectRequested {
                disconnectPolls += 1
                if scenario == "pending_uploads" {
                    connection.state = "disconnecting"
                    connection.message = "Waiting for pending uploads; cached changes are preserved."
                } else if scenario == "busy_mount" {
                    connection.state = "connected"
                    connection.message = "Drive is busy. Close its open files, then disconnect again."
                } else if disconnectPolls > 2 {
                    connection.state = "disconnected"
                    connection.mounted = false
                }
            }
            let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(connection))
            return try data(["ok": true, "serviceRunning": true, "launchAtLogin": false,
                             "dependencies": [:], "profiles": [],
                             "connections": scenario == "removed" ? [] : [object]])
        case "disconnect":
            connection.desiredConnected = false
            connection.state = "disconnecting"
            disconnectRequested = true
            if scenario == "disconnect_response_lost" {
                // The local command changed persisted intent, but its response
                // failed. Recovery must not leave the user's drive ejecting.
                throw TurtleError(message: "The service response was interrupted.")
            }
            return try data(["ok": true])
        case "access":
            unsafeAccess = connection.isMounted || connection.desiredConnected || connection.state != "disconnected"
            if unsafeAccess { return try data(["ok": false, "error": "Unsafe access change"] ) }
            if scenario == "access_rejected" {
                return try data(["ok": false, "error": "Upload pending cached changes before changing this drive's access"])
            }
            connection.readOnly = arguments.contains("--read-only")
            return try data(["ok": true])
        case "connect":
            connection.desiredConnected = true
            connection.state = "connected"
            connection.mounted = true
            connection.message = nil
            disconnectRequested = false
            return try data(["ok": true])
        default:
            throw TurtleError(message: "Unexpected command in test transport")
        }
    }
}

@MainActor
final class AppModel {
    var activeAction: String?
    var driveMessage: String?
    var error: String?
    var connections: [Connection] = []

    func refresh() async {
        if let data = try? await ServiceClient.run(["status"]),
           let status = try? JSONDecoder().decode(StatusResponse.self, from: data) {
            connections = status.connections
        }
    }

__UPDATE_DRIVE__
}

@main
enum Runner {
    @MainActor static func main() async throws {
        ServiceClient.scenario = CommandLine.arguments[1]
        let scenario = ServiceClient.scenario
        if !["disconnected", "already_busy", "removed"].contains(scenario) {
            ServiceClient.connection.desiredConnected = true
            ServiceClient.connection.state = "connected"
            ServiceClient.connection.mounted = true
        }
        if scenario == "still_connecting" {
            ServiceClient.connection.state = "connecting"
            ServiceClient.connection.mounted = false
        }
        if scenario == "mounted_without_intent" { ServiceClient.connection.desiredConnected = false }
        if scenario == "read_write" { ServiceClient.connection.readOnly = true }
        var presented = ServiceClient.connection
        if scenario == "stale_snapshot" {
            presented.state = "disconnected"
            presented.desiredConnected = false
            presented.mounted = false
        }
        let model = AppModel()
        if scenario == "already_busy" { model.activeAction = "login" }
        let result = await model.updateDrive(presented,
            arguments: ["access", presented.id, scenario == "read_write" ? "--read-write" : "--read-only"])
        let output: [String: Any] = [
            "success": result, "calls": ServiceClient.calls, "unsafeAccess": ServiceClient.unsafeAccess,
            "readOnly": ServiceClient.connection.readOnly,
            "desiredConnected": ServiceClient.connection.desiredConnected,
            "state": ServiceClient.connection.state,
            "activeAction": model.activeAction as Any? ?? NSNull(),
            "error": model.error as Any? ?? NSNull(),
            "message": model.driveMessage as Any? ?? NSNull(),
        ]
        print(String(decoding: try JSONSerialization.data(withJSONObject: output), as: UTF8.self))
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires macOS SwiftUI")
class DriveAccessNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "Sources/MountainTurtle.swift").read_text()
        models = source[source.index("struct Connection:"):source.index("enum ServiceClient {")]
        method_start = source.index("    func updateDrive(")
        method_end = source.index("\n}\n\nstruct BrandIcon", method_start)
        method = source[method_start:method_end]
        cls.directory = tempfile.TemporaryDirectory(prefix="mountainturtle-access-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        temporary = Path(cls.directory.name)
        harness = temporary / "AccessTests.swift"
        harness.write_text(HARNESS.replace("__RESPONSE_MODELS__", models).replace("__UPDATE_DRIVE__", method))
        cls.executable = temporary / "access-tests"
        result = subprocess.run([shutil.which("swiftc"), "-swift-version", "5", "-parse-as-library",
                                 str(harness), "-o", str(cls.executable)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError("Native access test compilation failed:\n" + result.stdout + result.stderr)

    def run_case(self, scenario):
        result = subprocess.run([str(self.executable), scenario], capture_output=True, text=True,
                                check=True, timeout=10)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["unsafeAccess"], payload)
        return payload, [arguments[0] for arguments in payload["calls"]]

    def test_disconnected_access_saves_without_connecting(self):
        result, calls = self.run_case("disconnected")
        self.assertTrue(result["success"])
        self.assertTrue(result["readOnly"])
        self.assertFalse(result["desiredConnected"])
        self.assertEqual(calls, ["status", "access", "status"])
        self.assertIsNone(result["activeAction"])

    def test_connected_access_waits_for_clean_detach_then_restores_connection(self):
        for scenario in ["connected", "stale_snapshot", "read_write", "mounted_without_intent"]:
            with self.subTest(scenario=scenario):
                result, calls = self.run_case(scenario)
                self.assertTrue(result["success"])
                self.assertEqual(result["readOnly"], scenario != "read_write")
                self.assertTrue(result["desiredConnected"])
                self.assertEqual(calls, ["status", "disconnect", "status", "status", "status", "access", "connect", "status"])
                self.assertIsNone(result["activeAction"])

    def test_pending_uploads_and_busy_mounts_preserve_access_and_restore_intent(self):
        for scenario in ["pending_uploads", "busy_mount"]:
            with self.subTest(scenario=scenario):
                result, calls = self.run_case(scenario)
                self.assertFalse(result["success"])
                self.assertFalse(result["readOnly"])
                self.assertTrue(result["desiredConnected"])
                self.assertNotIn("access", calls)
                self.assertIn("connect", calls)
                self.assertIsNotNone(result["error"])
                self.assertIsNone(result["activeAction"])
                self.assertLess(calls.count("status"), 10, "Recognized blocked ejections should not wait for timeout")
                self.assertIn("pending uploads" if scenario == "pending_uploads" else "busy", result["error"])

    def test_access_rejection_restores_previous_connection_and_setting(self):
        result, calls = self.run_case("access_rejected")
        self.assertFalse(result["success"])
        self.assertFalse(result["readOnly"])
        self.assertTrue(result["desiredConnected"])
        self.assertEqual(calls[-3:], ["access", "connect", "status"])
        self.assertIn("pending cached changes", result["error"])

    def test_interrupted_disconnect_response_restores_connection_intent(self):
        result, calls = self.run_case("disconnect_response_lost")
        self.assertFalse(result["success"])
        self.assertFalse(result["readOnly"])
        self.assertTrue(result["desiredConnected"])
        self.assertNotIn("access", calls)
        self.assertEqual(calls, ["status", "disconnect", "connect", "status"])
        self.assertIn("interrupted", result["error"])

    def test_in_progress_drive_is_left_untouched(self):
        result, calls = self.run_case("still_connecting")
        self.assertFalse(result["success"])
        self.assertEqual(calls, ["status", "status"])
        self.assertTrue(result["desiredConnected"])
        self.assertFalse(result["readOnly"])
        self.assertIn("finish connecting", result["error"])

    def test_another_active_action_is_not_overwritten(self):
        result, calls = self.run_case("already_busy")
        self.assertFalse(result["success"])
        self.assertEqual(calls, [])
        self.assertEqual(result["activeAction"], "login")

    def test_removed_connection_cannot_be_changed(self):
        result, calls = self.run_case("removed")
        self.assertFalse(result["success"])
        self.assertEqual(calls, ["status", "status"])
        self.assertIn("removed", result["error"])


if __name__ == "__main__":
    unittest.main()
