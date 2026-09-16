import Foundation
import CryptoKit
import CommonCrypto
import Security

/// Password-protected portable connections. Settings are validated by the service
/// before export and again before an import is saved.
enum ConnectionEncryption {
    static let minimumPasswordLength = 12
    static let maximumPasswordBytes = 1_024
    static let maximumSavedPasswordBytes = 16_384
    static let maximumDocumentBytes = 64 * 1_024
    static let maximumFileBytes = 256 * 1_024

    private static let format = "io.mountainturtle.encrypted-connection"
    private static let version = 1
    private static let cipher = "AES-256-GCM"
    private static let kdf = "PBKDF2-HMAC-SHA256"
    private static let iterations = 600_000
    private static let saltBytes = 16
    // Fixed metadata is authenticated in addition to AES-GCM's nonce and ciphertext.
    private static let authenticatedData = Data("io.mountainturtle.encrypted-connection\n1\nAES-256-GCM\nPBKDF2-HMAC-SHA256\n600000".utf8)

    struct Payload: Codable {
        let document: Data
        let sftpPassword: String?

        private enum CodingKeys: String, CodingKey, CaseIterable { case document, sftpPassword }

        init(document: Data, sftpPassword: String?) {
            self.document = document
            self.sftpPassword = sftpPassword
        }

        init(from decoder: Decoder) throws {
            try rejectUnknownKeys(decoder, allowing: CodingKeys.allCases.map(\.rawValue))
            let values = try decoder.container(keyedBy: CodingKeys.self)
            document = try values.decode(Data.self, forKey: .document)
            sftpPassword = try values.decodeIfPresent(String.self, forKey: .sftpPassword)
        }
    }

    enum Failure: LocalizedError {
        case exportPassword, tooLarge, invalidFile, invalidSavedPassword, encryptionFailed, unlockFailed

        var errorDescription: String? {
            switch self {
            case .exportPassword:
                return "Use an export password with at least 12 characters and no more than 1,024 bytes."
            case .tooLarge:
                return "This connection file is too large to transfer."
            case .invalidFile:
                return "This is not a supported password-protected Mountain Turtle connection file."
            case .invalidSavedPassword:
                return "The saved connection password is invalid or too long to transfer."
            case .encryptionFailed:
                return "The connection file could not be encrypted. Try exporting again."
            case .unlockFailed:
                return "The connection file could not be unlocked. Check the password; the file may also be damaged."
            }
        }
    }

    private struct Envelope: Codable {
        let format: String
        let version: Int
        let cipher: String
        let kdf: String
        let iterations: Int
        let salt: Data
        let sealedBox: Data

        private enum CodingKeys: String, CodingKey, CaseIterable {
            case format, version, cipher, kdf, iterations, salt, sealedBox
        }

        init(salt: Data, sealedBox: Data) {
            format = ConnectionEncryption.format
            version = ConnectionEncryption.version
            cipher = ConnectionEncryption.cipher
            kdf = ConnectionEncryption.kdf
            iterations = ConnectionEncryption.iterations
            self.salt = salt
            self.sealedBox = sealedBox
        }

        init(from decoder: Decoder) throws {
            try rejectUnknownKeys(decoder, allowing: CodingKeys.allCases.map(\.rawValue))
            let values = try decoder.container(keyedBy: CodingKeys.self)
            format = try values.decode(String.self, forKey: .format)
            version = try values.decode(Int.self, forKey: .version)
            cipher = try values.decode(String.self, forKey: .cipher)
            kdf = try values.decode(String.self, forKey: .kdf)
            iterations = try values.decode(Int.self, forKey: .iterations)
            salt = try values.decode(Data.self, forKey: .salt)
            sealedBox = try values.decode(Data.self, forKey: .sealedBox)
        }
    }

    private struct AnyKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { return nil }
    }

    private static func rejectUnknownKeys(_ decoder: Decoder, allowing keys: [String]) throws {
        let values = try decoder.container(keyedBy: AnyKey.self)
        guard Set(values.allKeys.map(\.stringValue)).isSubset(of: Set(keys)) else {
            throw Failure.invalidFile
        }
    }

    /// Identifies the file type only; decrypt performs all validation.
    static func isEncrypted(_ data: Data) -> Bool {
        guard !data.isEmpty, data.count <= maximumFileBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return object["format"] as? String == format
    }

    static func encrypt(document: Data, sftpPassword: String?, password: String) throws -> Data {
        guard password.count >= minimumPasswordLength, password.utf8.count <= maximumPasswordBytes else {
            throw Failure.exportPassword
        }
        let payload = Payload(document: document, sftpPassword: sftpPassword)
        try validate(payload)
        do {
            let salt = try randomSalt()
            let key = try deriveKey(password: password, salt: salt)
            let plaintext = try JSONEncoder().encode(payload)
            // CryptoKit generates a new random 96-bit nonce for each sealed box.
            let sealed = try AES.GCM.seal(plaintext, using: key, authenticating: authenticatedData)
            guard let combined = sealed.combined else { throw Failure.encryptionFailed }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(Envelope(salt: salt, sealedBox: combined))
            guard data.count <= maximumFileBytes else { throw Failure.tooLarge }
            return data
        } catch let failure as Failure {
            throw failure
        } catch {
            throw Failure.encryptionFailed
        }
    }

    static func decrypt(_ data: Data, password: String) throws -> Payload {
        guard !data.isEmpty else { throw Failure.invalidFile }
        guard data.count <= maximumFileBytes else { throw Failure.tooLarge }
        let envelope: Envelope
        do { envelope = try JSONDecoder().decode(Envelope.self, from: data) }
        catch { throw Failure.invalidFile }
        // Validate all attacker-supplied metadata before doing password work.
        // The work factor is fixed for this format version, never chosen by a file.
        guard envelope.format == format, envelope.version == version,
              envelope.cipher == cipher, envelope.kdf == kdf,
              envelope.iterations == iterations, envelope.salt.count == saltBytes,
              envelope.sealedBox.count > 12 + 16 else { throw Failure.invalidFile }
        guard !password.isEmpty, password.utf8.count <= maximumPasswordBytes else { throw Failure.unlockFailed }
        do {
            let key = try deriveKey(password: password, salt: envelope.salt)
            let sealed = try AES.GCM.SealedBox(combined: envelope.sealedBox)
            let plaintext = try AES.GCM.open(sealed, using: key, authenticating: authenticatedData)
            let payload = try JSONDecoder().decode(Payload.self, from: plaintext)
            try validate(payload)
            return payload
        } catch {
            // Do not expose whether the password, authenticated contents or tag was wrong.
            throw Failure.unlockFailed
        }
    }

    private static func validate(_ payload: Payload) throws {
        guard !payload.document.isEmpty else { throw Failure.invalidFile }
        guard payload.document.count <= maximumDocumentBytes else { throw Failure.tooLarge }
        if let savedPassword = payload.sftpPassword {
            guard savedPassword.utf8.count <= maximumSavedPasswordBytes,
                  !savedPassword.contains("\r"), !savedPassword.contains("\n"), !savedPassword.contains("\0") else {
                throw Failure.invalidSavedPassword
            }
        }
    }

    private static func randomSalt() throws -> Data {
        var bytes = [UInt8](repeating: 0, count: saltBytes)
        let result = bytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, buffer.count, buffer.baseAddress!)
        }
        guard result == errSecSuccess else { throw Failure.encryptionFailed }
        return Data(bytes)
    }

    private static func deriveKey(password: String, salt: Data) throws -> SymmetricKey {
        var passwordBytes = Array(password.utf8)
        var keyBytes = [UInt8](repeating: 0, count: 32)
        defer {
            _ = passwordBytes.withUnsafeMutableBytes { $0.initializeMemory(as: UInt8.self, repeating: 0) }
            _ = keyBytes.withUnsafeMutableBytes { $0.initializeMemory(as: UInt8.self, repeating: 0) }
        }
        let result = passwordBytes.withUnsafeBytes { passwordBuffer in
            salt.withUnsafeBytes { saltBuffer in
                keyBytes.withUnsafeMutableBytes { keyBuffer in
                    CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2),
                        passwordBuffer.bindMemory(to: Int8.self).baseAddress, passwordBuffer.count,
                        saltBuffer.bindMemory(to: UInt8.self).baseAddress, saltBuffer.count,
                        CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), UInt32(iterations),
                        keyBuffer.bindMemory(to: UInt8.self).baseAddress, keyBuffer.count)
                }
            }
        }
        guard result == kCCSuccess else { throw Failure.encryptionFailed }
        return SymmetricKey(data: keyBytes)
    }
}
