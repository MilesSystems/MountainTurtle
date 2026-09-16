import SwiftUI
import AppKit
import UniformTypeIdentifiers

extension UTType {
    static let mountainTurtleConnection = UTType(exportedAs: "io.mountainturtle.connection", conformingTo: .json)
}

struct ConnectionTransferRequest: Identifiable {
    enum Kind { case export(Connection), importFile(Data) }
    let id = UUID()
    let kind: Kind
}

struct PortableConnection: Decodable {
    let name: String
    let backend: String
    let readOnly: Bool
    let cacheMaxSizeMiB: Int
    let cacheMaxAgeHours: Int
    var bucket: String?
    var profile: String?
    var region: String?
    var host: String?
    var user: String?
    var port: Int?
    var remotePath: String?
    var authMode: String?

    var connection: Connection {
        Connection(id: UUID().uuidString, name: name, bucket: bucket ?? "", profile: profile ?? "",
                   region: region ?? "", readOnly: readOnly, autoConnect: false,
                   desiredConnected: false, state: "disconnected", mountPath: "",
                   cacheMaxSizeMiB: cacheMaxSizeMiB, cacheMaxAgeHours: cacheMaxAgeHours,
                   backend: backend, host: host, user: user, port: port,
                   remotePath: remotePath, authMode: authMode)
    }
}

struct ConnectionImportDraft {
    var connection: Connection
    var password: String?
}

enum ConnectionTransferIO {
    static func read(_ url: URL) throws -> Data {
        guard url.isFileURL, url.pathExtension.lowercased() == "mountainturtle" else {
            throw TurtleError(message: "Choose a Mountain Turtle connection file ending in .mountainturtle.")
        }
        let accessing = url.startAccessingSecurityScopedResource()
        defer { if accessing { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true else { throw TurtleError(message: "Choose a connection file, not a folder.") }
        guard (values.fileSize ?? 0) <= ConnectionEncryption.maximumFileBytes else {
            throw TurtleError(message: "This connection file is too large.")
        }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: ConnectionEncryption.maximumFileBytes + 1) ?? Data()
        guard !data.isEmpty, data.count <= ConnectionEncryption.maximumFileBytes else {
            throw TurtleError(message: "This connection file is empty or too large.")
        }
        return data
    }

    static func inspect(_ document: Data, password: String? = nil) async throws -> ConnectionImportDraft {
        guard !document.isEmpty, document.count <= ConnectionEncryption.maximumDocumentBytes else {
            throw TurtleError(message: "This connection file is empty or too large.")
        }
        struct Inspection: Decodable { let connection: PortableConnection }
        let response = try await ServiceClient.run(["inspect-connection"], standardInput: document)
        let portable = try JSONDecoder().decode(Inspection.self, from: response).connection
        guard password == nil || (portable.backend == "sftp" && portable.authMode == "password") else {
            throw TurtleError(message: "The saved password does not match this connection's authentication type.")
        }
        return ConnectionImportDraft(connection: portable.connection, password: password)
    }

    static func savedPassword(for connection: Connection) async throws -> String {
        guard connection.isSFTP, connection.authMode == "password", UUID(uuidString: connection.id) != nil else {
            throw TurtleError(message: "This connection does not use a saved SFTP password.")
        }
        let helper = ServiceClient.resources.deletingLastPathComponent()
            .appendingPathComponent("Helpers/Mountain Turtle Credentials")
        return try await Task.detached(priority: .userInitiated) {
            let process = Process(), output = Pipe()
            process.executableURL = helper
            process.arguments = ["get", connection.id]
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            try process.run()
            let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
            DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 30, execute: timeout)
            defer { timeout.cancel() }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0, !data.isEmpty, data.count <= 16_384,
                  let password = String(data: data, encoding: .utf8),
                  !password.contains("\0"), !password.contains("\r"), !password.contains("\n") else {
                throw TurtleError(message: "The saved SFTP password is unavailable. Unlock your login Keychain or save the password again, then retry the export.")
            }
            return password
        }.value
    }

    static func availableName(_ name: String, among connections: [Connection]) -> String {
        let used = Set(connections.map { $0.name.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX")) })
        func exists(_ value: String) -> Bool { used.contains(value.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))) }
        guard exists(name) else { return name }
        var index = 2
        while true {
            let suffix = " (\(index))"
            var prefix = name
            while prefix.utf8.count + suffix.utf8.count > 180 { prefix.removeLast() }
            let candidate = prefix + suffix
            if !exists(candidate) { return candidate }
            index += 1
        }
    }
}

@MainActor extension AppModel {
    func exportConnection(_ connection: Connection) {
        guard transferRequest == nil, activeAction == nil, !otherSheetPresented else { return }
        transferRequest = ConnectionTransferRequest(kind: .export(connection))
    }

    func chooseConnectionFile() {
        guard transferRequest == nil, activeAction == nil, !otherSheetPresented else { return }
        let panel = NSOpenPanel()
        panel.title = "Import connection"
        panel.prompt = "Review connection"
        panel.allowedContentTypes = [.mountainTurtleConnection]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        activeAction = "choosing-connection"
        panel.begin { response in
            Task { @MainActor in
                self.activeAction = nil
                guard response == .OK, let url = panel.url else { return }
                self.receiveConnectionFiles([url])
            }
        }
    }

    func receiveConnectionFiles(_ urls: [URL]) {
        guard urls.count == 1, let url = urls.first else {
            error = "Import one connection file at a time."
            return
        }
        guard transferRequest == nil, activeAction == nil, !otherSheetPresented else {
            error = "Finish the current connection action, then import the file again."
            return
        }
        activeAction = "reading-connection"
        Task {
            defer { activeAction = nil }
            do {
                let data = try await Task.detached(priority: .userInitiated) { try ConnectionTransferIO.read(url) }.value
                guard transferRequest == nil, !otherSheetPresented else {
                    throw TurtleError(message: "Close the current dialog, then import the connection file again.")
                }
                transferRequest = ConnectionTransferRequest(kind: .importFile(data))
            } catch { self.error = error.localizedDescription }
        }
    }
}

struct ConnectionExportView: View {
    @ObservedObject var model: AppModel
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var protect = false
    @State private var password = ""
    @State private var confirmation = ""
    @State private var working = false
    @State private var failure: String?

    private var includesPassword: Bool { connection.isSFTP && connection.authMode == "password" }
    private var valid: Bool {
        !protect || (password.count >= ConnectionEncryption.minimumPasswordLength
            && password.utf8.count <= ConnectionEncryption.maximumPasswordBytes && password == confirmation)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("Export connection", systemImage: "square.and.arrow.up").font(.title2.weight(.semibold))
            Text(connection.name).font(.headline)
            Picker("Include in this file", selection: $protect) {
                Text("Connection settings only").tag(false)
                Text("Password-protected file").tag(true)
            }.pickerStyle(.radioGroup).disabled(working)
            Text(protect
                 ? (includesPassword ? "Encrypt the connection settings and saved SFTP password. Share the export password separately with the person importing this file." : "Encrypt the connection settings. This connection has no saved SFTP password to include.")
                 : "Save the connection settings without passwords. Sign in again on the other Mac.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if protect {
                SecureField("Export password", text: $password).disabled(working)
                SecureField("Confirm export password", text: $confirmation).disabled(working)
                Text("Use at least 12 characters. You will need this password to import the file.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text("AWS sign-in, SSH private keys, and trusted server files stay on this Mac. Set those up on the receiving Mac when needed.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let failure { Text(failure).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Button("Cancel", role: .cancel) { password = ""; confirmation = ""; dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if working { ProgressView().controlSize(.small) }
                Button("Export…") { export() }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(!valid)
            }.disabled(working)
        }.textFieldStyle(.roundedBorder).padding(28).frame(width: 510).tint(moss)
            .interactiveDismissDisabled(working)
    }

    private func export() {
        guard valid, !working else { return }
        let shouldProtect = protect
        let passphrase = password
        working = true; failure = nil
        let panel = NSSavePanel()
        panel.title = "Export connection"
        panel.nameFieldStringValue = connection.name + ".mountainturtle"
        panel.allowedContentTypes = [.mountainTurtleConnection]
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        panel.begin { response in
            Task { @MainActor in
                defer { working = false }
                guard response == .OK, let url = panel.url else { return }
                do {
                    let document = try await ServiceClient.run(["export-connection", connection.id])
                    var result = document
                    if shouldProtect {
                        let secret = includesPassword ? try await ConnectionTransferIO.savedPassword(for: connection) : nil
                        result = try await Task.detached(priority: .userInitiated) {
                            try ConnectionEncryption.encrypt(document: document, sftpPassword: secret, password: passphrase)
                        }.value
                    }
                    // Write through a private temporary file in the destination directory;
                    // even settings-only exports contain account and server information.
                    let staging = url.deletingLastPathComponent().appendingPathComponent(".mountainturtle-\(UUID().uuidString).tmp")
                    defer { try? FileManager.default.removeItem(at: staging) }
                    guard FileManager.default.createFile(atPath: staging.path, contents: result, attributes: [.posixPermissions: 0o600]) else {
                        throw TurtleError(message: "The connection file could not be saved in this folder.")
                    }
                    if FileManager.default.fileExists(atPath: url.path) {
                        _ = try FileManager.default.replaceItemAt(url, withItemAt: staging, options: .usingNewMetadataOnly)
                    } else { try FileManager.default.moveItem(at: staging, to: url) }
                    password = ""; confirmation = ""
                    model.driveMessage = "Connection exported. Drag the .mountainturtle file into Mountain Turtle on the other Mac to import it."
                    dismiss()
                } catch { failure = error.localizedDescription }
            }
        }
    }
}

struct ConnectionImportView: View {
    @ObservedObject var model: AppModel
    let data: Data
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var working = false
    @State private var failure: String?
    @State private var draft: ConnectionImportDraft?

    var body: some View {
        Group {
            if let draft {
                ConnectionEditor(model: model, original: nil, imported: draft)
            } else {
                VStack(alignment: .leading, spacing: 20) {
                    Label("Import connection", systemImage: "square.and.arrow.down").font(.title2.weight(.semibold))
                    if ConnectionEncryption.isEncrypted(data) {
                        Text("This file is password-protected. Enter its export password to review the connection.")
                            .font(.callout).foregroundStyle(.secondary)
                        SecureField("Export password", text: $password).disabled(working)
                    } else if failure == nil {
                        Text("Checking connection settings…").font(.callout).foregroundStyle(.secondary)
                    }
                    if let failure { Text(failure).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
                    HStack {
                        Button("Cancel", role: .cancel) { password = ""; dismiss() }.keyboardShortcut(.cancelAction).disabled(working)
                        Spacer()
                        if working { ProgressView().controlSize(.small) }
                        if ConnectionEncryption.isEncrypted(data) {
                            Button("Unlock") { inspect() }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                                .disabled(working || password.isEmpty || password.utf8.count > ConnectionEncryption.maximumPasswordBytes)
                        }
                    }
                }.padding(28).frame(width: 510).textFieldStyle(.roundedBorder).tint(moss)
            }
        }.interactiveDismissDisabled(working)
            .task { if !ConnectionEncryption.isEncrypted(data) { inspect() } }
    }

    private func inspect() {
        working = true; failure = nil
        Task {
            defer { working = false }
            do {
                var document = data
                var secret: String?
                if ConnectionEncryption.isEncrypted(data) {
                    let passphrase = password
                    let payload = try await Task.detached(priority: .userInitiated) {
                        try ConnectionEncryption.decrypt(data, password: passphrase)
                    }.value
                    document = payload.document; secret = payload.sftpPassword
                }
                var imported = try await ConnectionTransferIO.inspect(document, password: secret)
                await model.refresh()
                imported.connection.name = ConnectionTransferIO.availableName(imported.connection.name, among: model.connections)
                password = ""
                draft = imported
            } catch { failure = error.localizedDescription }
        }
    }
}
