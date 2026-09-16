import Foundation

private struct TransferTestFailure: Error, CustomStringConvertible {
    let description: String
}

@main
private enum ConnectionTransferTests {
    static func main() async {
        do {
            guard CommandLine.arguments.count == 2 else { throw TransferTestFailure(description: "Missing test case") }
            switch CommandLine.arguments[1] {
            case "inspection_limits": try await inspectionLimits()
            case "inspection_boundary": try await inspectionBoundary()
            case "file_types": try fileTypes()
            case "file_limits": try fileLimits()
            case "duplicate_names": try duplicateNames()
            case "unicode_names": try unicodeNames()
            default: throw TransferTestFailure(description: "Unknown test case")
            }
            print("PASS \(CommandLine.arguments[1])")
        } catch {
            FileHandle.standardError.write(Data("FAIL: \(error)\n".utf8))
            exit(1)
        }
    }

    private static func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
        guard value() else { throw TransferTestFailure(description: message) }
    }

    private static func failure(_ message: String, _ operation: () throws -> Void) throws {
        do { try operation() }
        catch let error as TurtleError {
            try require(error.localizedDescription == message, "Unexpected error: \(error.localizedDescription)")
            return
        }
        throw TransferTestFailure(description: "Operation did not return the expected file error")
    }

    private static func inspectionFailure(_ data: Data, expected message: String) async throws {
        do { _ = try await ConnectionTransferIO.inspect(data) }
        catch let error as TurtleError {
            try require(error.localizedDescription == message, "Unexpected inspection error: \(error.localizedDescription)")
            return
        }
        throw TransferTestFailure(description: "Inspection did not return the expected error")
    }

    private static func assertNoServiceFixture() throws {
        // A test harness has no packaged service. If the input guard regresses,
        // ServiceClient returns its missing-service error instead of this guard's
        // error. It can never reach a real store, credential helper, or mount.
        let script = ServiceClient.resources.appendingPathComponent("service/turtle_service.py")
        try require(!FileManager.default.fileExists(atPath: script.path), "Test harness unexpectedly contains a service")
    }

    private static func inspectionLimits() async throws {
        try assertNoServiceFixture()
        for count in [0, 65_537, 262_144] {
            try await inspectionFailure(Data(repeating: 120, count: count),
                                        expected: "This connection file is empty or too large.")
        }
    }

    private static func inspectionBoundary() async throws {
        try assertNoServiceFixture()
        // Exactly 64 KiB must pass the native limit and reach the service layer.
        try await inspectionFailure(Data(repeating: 120, count: 65_536),
                                    expected: "The mount service is missing from this app. Rebuild Mountain Turtle with the included build script.")
    }

    private static func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("mountainturtle-transfer-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private static func fileTypes() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let extensionError = "Choose a Mountain Turtle connection file ending in .mountainturtle."
        try failure(extensionError) { _ = try ConnectionTransferIO.read(URL(string: "https://example.test/fixture.mountainturtle")!) }
        let unrelated = root.appendingPathComponent("fixture.json")
        try Data("fixture".utf8).write(to: unrelated)
        try failure(extensionError) { _ = try ConnectionTransferIO.read(unrelated) }
        let directory = root.appendingPathComponent("folder.mountainturtle", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try failure("Choose a connection file, not a folder.") { _ = try ConnectionTransferIO.read(directory) }
        let valid = root.appendingPathComponent("fixture.MOUNTAINTURTLE")
        let data = Data("synthetic transfer fixture".utf8)
        try data.write(to: valid)
        let result = try ConnectionTransferIO.read(valid)
        try require(result == data, "A regular file with the allowed extension was changed")
    }

    private static func fileLimits() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let empty = root.appendingPathComponent("empty.mountainturtle")
        try Data().write(to: empty)
        try failure("This connection file is empty or too large.") { _ = try ConnectionTransferIO.read(empty) }
        let oversized = root.appendingPathComponent("oversized.mountainturtle")
        try Data(repeating: 120, count: 262_145).write(to: oversized)
        try failure("This connection file is too large.") { _ = try ConnectionTransferIO.read(oversized) }
        let file = root.appendingPathComponent("boundary.mountainturtle")
        let boundary = Data(repeating: 120, count: 262_144)
        try boundary.write(to: file)
        let result = try ConnectionTransferIO.read(file)
        try require(result == boundary, "File read truncated a file at the wrapper limit")
    }

    private static func connection(_ name: String) -> Connection {
        Connection(id: UUID().uuidString, name: name, bucket: "synthetic-bucket", profile: "fixture",
                   region: "us-east-1", readOnly: true, autoConnect: false, desiredConnected: false,
                   state: "disconnected", mountPath: "")
    }

    private static func duplicateNames() throws {
        let originals = [connection("PHOTOS"), connection("Photos (2)"), connection("Photos (3)")]
        let snapshot = originals
        try require(ConnectionTransferIO.availableName("Photos", among: originals) == "Photos (4)", "Duplicate name was not advanced")
        try require(ConnectionTransferIO.availableName("Other drive", among: originals) == "Other drive", "Unique name was changed")
        try require(ConnectionTransferIO.availableName("été", among: [connection("ÉTÉ")]) == "été (2)", "Unicode case collision was missed")
        try require(originals == snapshot, "Name selection changed an existing connection")
    }

    private static func unicodeNames() throws {
        for name in [String(repeating: "é", count: 90), String(repeating: "🐢", count: 45), String(repeating: "e\u{301}", count: 60)] {
            try require(name.utf8.count == 180, "Invalid Unicode boundary fixture")
            let original = connection(name)
            let first = ConnectionTransferIO.availableName(name, among: [original])
            try require(first.hasSuffix(" (2)") && first.utf8.count <= 180, "Duplicate exceeded UTF-8 name limit")
            try require(name.hasPrefix(String(first.dropLast(4))), "Name lost valid grapheme boundaries")
            let duplicateNames = (2...9).map { index -> Connection in
                var prefix = name
                let suffix = " (\(index))"
                while prefix.utf8.count + suffix.utf8.count > 180 { prefix.removeLast() }
                return connection(prefix + suffix)
            }
            let tenth = ConnectionTransferIO.availableName(name, among: [original] + duplicateNames)
            try require(tenth.hasSuffix(" (10)") && tenth.utf8.count <= 180, "Longer duplicate suffix exceeded UTF-8 limit")
            try require(!duplicateNames.map(\.name).contains(tenth), "Duplicate suffix reused an existing name")
        }
    }
}
