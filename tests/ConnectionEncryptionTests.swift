import Foundation
import CryptoKit

private struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

@main
private enum ConnectionEncryptionTests {
    private static let password = "correct horse battery staple"
    private static let document = Data("{\"format\":\"io.mountainturtle.connection\",\"version\":1,\"connection\":{\"name\":\"Photos\"}}".utf8)

    static func main() {
        do {
            guard CommandLine.arguments.count == 2 else { throw TestFailure(description: "Missing test case") }
            switch CommandLine.arguments[1] {
            case "roundtrip": try roundTrip()
            case "unicode": try unicode()
            case "tampering": try tampering()
            case "metadata": try metadata()
            case "types": try types()
            case "limits": try limits()
            case "saved_password": try savedPassword()
            case "reference": try reference()
            default: throw TestFailure(description: "Unknown test case")
            }
            print("PASS \(CommandLine.arguments[1])")
        } catch {
            FileHandle.standardError.write(Data("FAIL: \(error)\n".utf8))
            exit(1)
        }
    }

    private static func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
        guard value() else { throw TestFailure(description: message) }
    }

    @discardableResult
    private static func failure(_ expected: ConnectionEncryption.Failure? = nil, _ body: () throws -> Void) throws -> String {
        do { try body() }
        catch let error as ConnectionEncryption.Failure {
            if let expected {
                try require(error.localizedDescription == expected.localizedDescription, "Unexpected failure: \(error)")
            }
            return error.localizedDescription
        }
        catch { throw TestFailure(description: "Unexpected error type: \(error)") }
        throw TestFailure(description: "Operation unexpectedly succeeded")
    }

    private static func exported() throws -> Data {
        try ConnectionEncryption.encrypt(document: document, sftpPassword: "saved-server-secret", password: password)
    }

    private static func mutate(_ data: Data, _ body: (inout [String: Any]) -> Void) throws -> Data {
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        body(&object)
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    private static func roundTrip() throws {
        let first = try exported(), second = try exported()
        try require(ConnectionEncryption.isEncrypted(first), "Encrypted file not identified")
        try require(!ConnectionEncryption.isEncrypted(document), "Settings file identified as encrypted")
        try require(first != second, "Identical encryption outputs")
        let firstObject = try JSONSerialization.jsonObject(with: first) as! [String: Any]
        let secondObject = try JSONSerialization.jsonObject(with: second) as! [String: Any]
        try require(firstObject["salt"] as? String != secondObject["salt"] as? String, "Salt reused")
        let firstBox = Data(base64Encoded: firstObject["sealedBox"] as! String)!
        let secondBox = Data(base64Encoded: secondObject["sealedBox"] as! String)!
        try require(firstBox.prefix(12) != secondBox.prefix(12), "Nonce reused")
        let text = String(decoding: first, as: UTF8.self)
        try require(!text.contains("saved-server-secret") && !text.contains("Photos"), "Plaintext leaked")
        let payload = try ConnectionEncryption.decrypt(first, password: password)
        try require(payload.document == document, "Document changed")
        try require(payload.sftpPassword == "saved-server-secret", "Saved password changed")
    }

    private static func unicode() throws {
        let exportPassword = "portable 🔒 password café"
        let secret = "  密碼 💾 café\tquotes\" slash\\  "
        let encrypted = try ConnectionEncryption.encrypt(document: document, sftpPassword: secret, password: exportPassword)
        let payload = try ConnectionEncryption.decrypt(encrypted, password: exportPassword)
        try require(payload.sftpPassword == secret, "Unicode password changed")
        let settings = try ConnectionEncryption.encrypt(document: document, sftpPassword: nil, password: exportPassword)
        let decryptedSettings = try ConnectionEncryption.decrypt(settings, password: exportPassword)
        try require(decryptedSettings.sftpPassword == nil, "Absent saved password was invented")
    }

    private static func tampering() throws {
        let encrypted = try exported()
        let wrongPassword = try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(encrypted, password: "different password") }
        // Includes nonce, encrypted payload and authentication tag mutations.
        for position in [0, 13, -1] {
            let changed = try mutate(encrypted) { object in
                var bytes = Data(base64Encoded: object["sealedBox"] as! String)!
                bytes[position < 0 ? bytes.count - 1 : position] ^= 1
                object["sealedBox"] = bytes.base64EncodedString()
            }
            let error = try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(changed, password: password) }
            try require(error == wrongPassword, "Tampering disclosed different error")
        }
        let changedSalt = try mutate(encrypted) { $0["salt"] = Data(repeating: 0, count: 16).base64EncodedString() }
        try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(changedSalt, password: password) }
    }

    private static func metadata() throws {
        let encrypted = try exported()
        let invalid: [(String, Any)] = [
            ("format", "other-application"), ("version", 2), ("cipher", "AES-128-GCM"),
            ("kdf", "SHA256"), ("iterations", 599_999), ("iterations", Int.max),
            ("salt", Data(repeating: 1, count: 15).base64EncodedString()),
            ("salt", Data(repeating: 1, count: 17).base64EncodedString()),
            ("salt", "not base64!"), ("sealedBox", Data(repeating: 1, count: 28).base64EncodedString()),
            ("sealedBox", "not base64!"), ("unknown", "unrecognized field")
        ]
        for (key, value) in invalid {
            let changed = try mutate(encrypted) { $0[key] = value }
            // Even an empty password must fail metadata validation first.
            try failure(.invalidFile) { _ = try ConnectionEncryption.decrypt(changed, password: "") }
        }
        for key in ["format", "version", "cipher", "kdf", "iterations", "salt", "sealedBox"] {
            let changed = try mutate(encrypted) { $0.removeValue(forKey: key) }
            try failure(.invalidFile) { _ = try ConnectionEncryption.decrypt(changed, password: "") }
        }
        for malformed in [Data(), Data("null".utf8), Data("[]".utf8), Data("{bad}".utf8), Data([0xFF])] {
            try require(!ConnectionEncryption.isEncrypted(malformed), "Malformed file identified as encrypted")
            try failure(.invalidFile) { _ = try ConnectionEncryption.decrypt(malformed, password: password) }
        }
    }

    private static func types() throws {
        let encrypted = try exported()
        for key in ["version", "iterations"] {
            for value: Any in [true, false, "1", NSNull(), 1.25, [1]] {
                let changed = try mutate(encrypted) { $0[key] = value }
                try failure(.invalidFile) { _ = try ConnectionEncryption.decrypt(changed, password: "") }
            }
        }
        for key in ["format", "cipher", "kdf", "salt", "sealedBox"] {
            for value: Any in [true, 1, NSNull(), ["text"]] {
                let changed = try mutate(encrypted) { $0[key] = value }
                try failure(.invalidFile) { _ = try ConnectionEncryption.decrypt(changed, password: "") }
            }
        }
    }

    private static func limits() throws {
        for invalid in ["", "12345678901", String(repeating: "a", count: 1_025), String(repeating: "🔒", count: 257)] {
            try failure(.exportPassword) { _ = try ConnectionEncryption.encrypt(document: document, sftpPassword: nil, password: invalid) }
        }
        for valid in ["123456789012", String(repeating: "a", count: 1_024)] {
            let encrypted = try ConnectionEncryption.encrypt(document: document, sftpPassword: nil, password: valid)
            let payload = try ConnectionEncryption.decrypt(encrypted, password: valid)
            try require(payload.document == document, "Password boundary failed")
        }
        let maximumDocument = Data(repeating: 65, count: ConnectionEncryption.maximumDocumentBytes)
        let encrypted = try ConnectionEncryption.encrypt(document: maximumDocument, sftpPassword: String(repeating: "\t", count: ConnectionEncryption.maximumSavedPasswordBytes), password: password)
        let payload = try ConnectionEncryption.decrypt(encrypted, password: password)
        try require(payload.document == maximumDocument, "Maximum document not preserved")
        try failure(.tooLarge) { _ = try ConnectionEncryption.encrypt(document: maximumDocument + Data([1]), sftpPassword: nil, password: password) }
        try failure(.invalidFile) { _ = try ConnectionEncryption.encrypt(document: Data(), sftpPassword: nil, password: password) }
        let tooLarge = Data(repeating: 32, count: ConnectionEncryption.maximumFileBytes + 1)
        try require(!ConnectionEncryption.isEncrypted(tooLarge), "Oversized file was parsed")
        try failure(.tooLarge) { _ = try ConnectionEncryption.decrypt(tooLarge, password: password) }
        try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(encrypted, password: String(repeating: "a", count: 1_025)) }
        try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(encrypted, password: "") }
    }

    private static func savedPassword() throws {
        let secret = String(repeating: "🔒", count: ConnectionEncryption.maximumSavedPasswordBytes / 4)
        let encrypted = try ConnectionEncryption.encrypt(document: document, sftpPassword: secret, password: password)
        let payload = try ConnectionEncryption.decrypt(encrypted, password: password)
        try require(payload.sftpPassword == secret, "Maximum saved password not preserved")
        for invalid in [secret + "a", "secret\rpassword", "secret\npassword", "secret\0password"] {
            try failure(.invalidSavedPassword) { _ = try ConnectionEncryption.encrypt(document: document, sftpPassword: invalid, password: password) }
        }
    }

    private static func reference() throws {
        // Independent reference from Python hashlib.pbkdf2_hmac("sha256",
        // "independent 🔒 password".encode(), bytes(range(16)), 600000, 32).
        let keyHex = "0c026502c5b656c5004d3660a5c0f15807444cc973ecaf551e458399cbaf56cb"
        var keyBytes = Data()
        for index in stride(from: 0, to: keyHex.count, by: 2) {
            let start = keyHex.index(keyHex.startIndex, offsetBy: index)
            keyBytes.append(UInt8(keyHex[start..<keyHex.index(start, offsetBy: 2)], radix: 16)!)
        }
        let salt = Data(0..<16)
        let context = Data("io.mountainturtle.encrypted-connection\n1\nAES-256-GCM\nPBKDF2-HMAC-SHA256\n600000".utf8)
        func referenceFile(payload: [String: Any], authentication: Data = context) throws -> Data {
            let plaintext = try JSONSerialization.data(withJSONObject: payload)
            let sealed = try AES.GCM.seal(plaintext, using: SymmetricKey(data: keyBytes),
                                          nonce: AES.GCM.Nonce(data: Data(0..<12)), authenticating: authentication)
            return try JSONSerialization.data(withJSONObject: [
                "format": "io.mountainturtle.encrypted-connection", "version": 1,
                "cipher": "AES-256-GCM", "kdf": "PBKDF2-HMAC-SHA256", "iterations": 600_000,
                "salt": salt.base64EncodedString(), "sealedBox": sealed.combined!.base64EncodedString()
            ])
        }
        let referencePassword = "independent 🔒 password"
        let referencePayload: [String: Any] = ["document": document.base64EncodedString(), "sftpPassword": "secret"]
        let encrypted = try referenceFile(payload: referencePayload)
        let payload = try ConnectionEncryption.decrypt(encrypted, password: referencePassword)
        try require(payload.document == document && payload.sftpPassword == "secret", "Independent KDF reference failed")
        let wrongContext = try referenceFile(payload: referencePayload, authentication: Data())
        try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(wrongContext, password: referencePassword) }
        // Validly authenticated payloads still have to pass shape and size checks.
        for malformed: [String: Any] in [
            ["document": document.base64EncodedString(), "sftpPassword": true],
            ["document": document.base64EncodedString(), "unexpected": "field"],
            ["document": "not base64"], ["document": ""],
            ["document": Data(repeating: 1, count: ConnectionEncryption.maximumDocumentBytes + 1).base64EncodedString()],
            ["document": document.base64EncodedString(), "sftpPassword": "line\nbreak"]
        ] {
            let invalid = try referenceFile(payload: malformed)
            try failure(.unlockFailed) { _ = try ConnectionEncryption.decrypt(invalid, password: referencePassword) }
        }
    }
}
