import Foundation
import Security

// Passwords cross process boundaries only through pipes, never command arguments.
func fail(_ message: String, _ status: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(status)
}

let arguments = CommandLine.arguments
guard arguments.count == 3, ["set", "get", "delete"].contains(arguments[1]),
      UUID(uuidString: arguments[2]) != nil else { fail("Invalid credential request.") }
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: "io.mountainturtle.sftp",
    kSecAttrAccount as String: arguments[2]
]

switch arguments[1] {
case "set":
    let password = FileHandle.standardInput.readData(ofLength: 16_385)
    guard !password.isEmpty, password.count <= 16_384,
          let value = String(data: password, encoding: .utf8), !value.contains("\0") else {
        fail("Enter a valid SFTP password.")
    }
    var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: password] as CFDictionary)
    if status == errSecItemNotFound {
        var item = query
        item[kSecValueData as String] = password
        item[kSecAttrLabel as String] = "Mountain Turtle SFTP"
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        status = SecItemAdd(item as CFDictionary, nil)
    }
    guard status == errSecSuccess else { fail("Could not save the SFTP password. Unlock your login keychain and try again.") }
case "get":
    var lookup = query
    lookup[kSecReturnData as String] = true
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &result)
    guard status == errSecSuccess, let password = result as? Data else {
        fail("SFTP password unavailable. Unlock your login keychain or save the password again.", status == errSecItemNotFound ? 2 : 1)
    }
    FileHandle.standardOutput.write(password)
case "delete":
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
        fail("Could not remove the saved SFTP password. Unlock your login keychain and try again.")
    }
default: fail("Invalid credential request.")
}
