import SwiftUI
import AppKit
import Combine
import FinderSync
import os

let moss = Color(red: 0.18, green: 0.37, blue: 0.29)
let cream = Color(red: 0.97, green: 0.97, blue: 0.94)
let ink = Color(red: 0.13, green: 0.20, blue: 0.17)
let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.6.0"

struct Connection: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var bucket: String
    var profile: String
    var region: String
    var readOnly: Bool
    var autoConnect: Bool
    var desiredConnected: Bool
    var state: String
    var message: String?
    var mountPath: String
    var updatedAt: Double?
    var mounted: Bool?
    var cacheMaxSizeMiB: Int?
    var cacheMaxAgeHours: Int?
    var sidebarError: String?
    var backend: String?
    var host: String?
    var user: String?
    var port: Int?
    var remotePath: String?
    var authMode: String?
    var keyFile: String?
    var knownHostsFile: String?
    var passwordConfigured: Bool?

    var isSFTP: Bool { backend == "sftp" }
    var supportsPhotoBrowser: Bool { !isSFTP }
    var authenticationLabel: String {
        switch authMode {
        case "password": return "Password in Keychain"
        case "keyFile": return "SSH private key"
        default: return "SSH agent"
        }
    }

    var isConnected: Bool { state == "connected" }
    var isMounted: Bool { mounted ?? isConnected }
    var isWorking: Bool { ["connecting", "disconnecting"].contains(state) }
    var title: String {
        switch state {
        case "connected": return "Connected"
        case "connecting": return "Connecting"
        case "disconnecting": return "Ejecting"
        case "needsLogin": return "Sign-in needed"
        case "error": return "Needs attention"
        default: return "Disconnected"
        }
    }
    var color: Color {
        switch state {
        case "connected": return moss
        case "error", "needsLogin": return .orange
        default: return .secondary
        }
    }
}

struct Dependencies: Decodable {
    var rclone: String?
    var aws: String?
    var python: String?
    var brew: String?
    var awsVersion: String?
    var awsCliV2: Bool?
    var rcloneVersion: String?
    var rcloneNfsmount: Bool?
    var appPath: String?
    var appInstalled: Bool?
    var privacyState: String?
    var privacyMessage: String?

    var awsReady: Bool { awsCliV2 == true }
    var rcloneReady: Bool { rcloneNfsmount == true }
    var installedReady: Bool { appInstalled == true }
    var privacyReady: Bool { privacyState == "approved" }
}

struct StatusResponse: Decodable {
    var ok: Bool
    var serviceRunning: Bool
    var launchAtLogin: Bool
    var dependencies: Dependencies
    var profiles: [String]
    var connections: [Connection]
}

struct ActionResponse: Decodable {
    var ok: Bool
    var id: String?
    var error: String?
    var message: String?
}

struct TurtleError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

enum ServiceClient {
    static let pythonPath: String? = {
        for candidate in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
            guard FileManager.default.isExecutableFile(atPath: candidate) else { continue }
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: candidate)
            probe.arguments = ["-c", "import sys, plistlib, ssl, fcntl; sys.exit(0 if sys.version_info >= (3, 9) else 1)"]
            probe.standardOutput = FileHandle.nullDevice
            probe.standardError = FileHandle.nullDevice
            do { try probe.run(); probe.waitUntilExit(); if probe.terminationStatus == 0 { return candidate } }
            catch { continue }
        }
        return nil
    }()

    static var resources: URL {
        Bundle.main.resourceURL ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources")
    }

    static func installCurrentAppToApplications() throws -> URL {
        let source = Bundle.main.bundleURL.standardizedFileURL
        let applications = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications", isDirectory: true)
        let destination = applications.appendingPathComponent("Mountain Turtle.app", isDirectory: true).standardizedFileURL
        if source.path == destination.path { return destination }
        try FileManager.default.createDirectory(at: applications, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: destination.path) {
            let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "")
            let backup = applications.appendingPathComponent("Mountain Turtle.backup-\(stamp).app", isDirectory: true)
            try FileManager.default.moveItem(at: destination, to: backup)
        }
        try FileManager.default.copyItem(at: source, to: destination)
        return destination
    }

    static func openPrivacySettings() {
        let primary = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders")
        if let primary, NSWorkspace.shared.open(primary) { return }
        if let fallback = URL(string: "x-apple.systempreferences:com.apple.preference.security") {
            NSWorkspace.shared.open(fallback)
        }
    }

    static func writeTerminalInstaller(includeAWS: Bool) throws -> URL {
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Mountain Turtle", isDirectory: true)
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        let script = support.appendingPathComponent("install-tools.sh")
        let content = """
        #!/bin/bash
        set -euo pipefail
        export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if ! command -v brew >/dev/null 2>&1; then
          /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
          if [ -x /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
          elif [ -x /usr/local/bin/brew ]; then
            eval "$(/usr/local/bin/brew shellenv)"
          fi
        fi
        brew install \(includeAWS ? "awscli rclone" : "rclone")
        echo
        echo "Mountain Turtle tools are installed. Return to Mountain Turtle and refresh setup."
        read -r -p "Press Return to close this window. "
        """
        try content.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        return script
    }

    static func openTerminalInstaller(includeAWS: Bool) throws {
        let script = try writeTerminalInstaller(includeAWS: includeAWS)
        let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app", isDirectory: true)
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        NSWorkspace.shared.open([script], withApplicationAt: terminal, configuration: configuration)
    }

    static func installToolsWithHomebrew(includeAWS: Bool, progress: @escaping @Sendable (String) -> Void) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let output = Pipe()
                let command = """
                export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                brew install \(includeAWS ? "awscli rclone" : "rclone")
                """
                process.executableURL = URL(fileURLWithPath: "/bin/zsh")
                process.arguments = ["-lc", command]
                process.standardOutput = output
                process.standardError = output
                let lock = NSLock()
                var finished = false
                func finish(_ result: Result<Void, Error>) {
                    lock.lock()
                    defer { lock.unlock() }
                    guard !finished else { return }
                    finished = true
                    output.fileHandleForReading.readabilityHandler = nil
                    switch result {
                    case .success: continuation.resume(returning: ())
                    case .failure(let error): continuation.resume(throwing: error)
                    }
                }
                output.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
                    progress(text)
                }
                process.terminationHandler = { proc in
                    if proc.terminationStatus == 0 { finish(.success(())) }
                    else { finish(.failure(TurtleError(message: "Homebrew could not install the selected tools. Open the Terminal installer and try again."))) }
                }
                do { try process.run() }
                catch { finish(.failure(error)) }
            }
        }
    }

    static func run(_ arguments: [String], standardInput: Data? = nil, scriptName: String = "turtle_service.py") async throws -> Data {
        let resources = self.resources
        let script = resources.appendingPathComponent("service/\(scriptName)")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw TurtleError(message: "The mount service is missing from this app. Rebuild Mountain Turtle with the included build script.")
        }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let output = Pipe(), errors = Pipe()
                process.standardOutput = output
                process.standardError = errors
                let input = standardInput.map { _ in Pipe() }
                process.standardInput = input ?? FileHandle.nullDevice
                do {
                    guard let python = pythonPath else { throw TurtleError(message: "A working Python 3.9 or newer is needed to run the mount service. See the setup guide.") }
                    process.executableURL = URL(fileURLWithPath: python)
                    process.arguments = ["-B", script.path, "--resource-dir", resources.path] + arguments
                    try process.run()
                    if let input, let standardInput {
                        try input.fileHandleForWriting.write(contentsOf: standardInput)
                        try input.fileHandleForWriting.close()
                    }
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    _ = errors.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if process.terminationStatus != 0 {
                        let decoded = try? JSONDecoder().decode(ActionResponse.self, from: data)
                        throw TurtleError(message: decoded?.error ?? "The mount service could not start. Check the setup guide and your Python installation.")
                    }
                    continuation.resume(returning: data)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
}

@MainActor final class AppModel: ObservableObject {
    static let shared = AppModel()
    @Published var connections: [Connection] = []
    @Published var profiles: [String] = []
    @Published var dependencies = Dependencies()
    @Published var selectedID: String?
    @Published var launchAtLogin = false
    @Published var finderBadgesEnabled = false
    @Published var isLoading = true
    @Published var activeAction: String?
    @Published var loginMessage: String?
    @Published var error: String?
    @Published var serviceError: String?
    @Published var drivePanel: DrivePanel?
    @Published var driveMessage: String?
    @Published var transferRequest: ConnectionTransferRequest?
    @Published var otherSheetPresented = false
    private var refreshing = false
    private var timer: Timer?

    var selected: Connection? { connections.first { $0.id == selectedID } }
    var connectedCount: Int { connections.filter(\.isConnected).count }
    var requiresAWS: Bool { connections.contains { !$0.isSFTP } }
    var missingTools: Bool { !isLoading && ((!dependencies.awsReady && requiresAWS) || !dependencies.rcloneReady) }
    var setupNeedsAttention: Bool {
        !isLoading && serviceError == nil && (!dependencies.installedReady || (requiresAWS && !dependencies.awsReady)
            || !dependencies.rcloneReady || dependencies.privacyState == "needsApproval")
    }

    func start() {
        guard timer == nil else { return }
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
            Task { @MainActor in await self.refresh() }
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        finderBadgesEnabled = FIFinderSyncController.isExtensionEnabled
        refreshing = true
        defer { refreshing = false; isLoading = false }
        do {
            let data = try await ServiceClient.run(["status"])
            let status = try JSONDecoder().decode(StatusResponse.self, from: data)
            connections = status.connections
            profiles = status.profiles
            dependencies = status.dependencies
            launchAtLogin = status.launchAtLogin
            if selectedID == nil || !connections.contains(where: { $0.id == selectedID }) {
                selectedID = connections.first?.id
            }
            serviceError = nil
        } catch { serviceError = error.localizedDescription }
    }

    @discardableResult func action(_ args: [String], standardInput: Data? = nil) async -> Bool {
        guard activeAction == nil else { return false }
        activeAction = args.first
        loginMessage = nil
        defer { activeAction = nil }
        do {
            let data = try await ServiceClient.run(args, standardInput: standardInput)
            let response = try JSONDecoder().decode(ActionResponse.self, from: data)
            guard response.ok else { throw TurtleError(message: response.error ?? "The action could not be completed.") }
            if let id = response.id { selectedID = id }
            await refresh()
            if args.first == "login" { loginMessage = "AWS sign-in completed." }
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func openFinder(_ connection: Connection) {
        guard connection.isMounted else { return }
        let url = URL(fileURLWithPath: connection.mountPath, isDirectory: true)
        // Launch Services can wait on NFS metadata while issuing a sandbox extension.
        // Ask Finder to reveal the volume without blocking the app's main thread.
        DispatchQueue.global(qos: .userInitiated).async {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func openGuide() {
        let guide = ServiceClient.resources.appendingPathComponent("README.md")
        if FileManager.default.fileExists(atPath: guide.path) { NSWorkspace.shared.open(guide) }
        else { error = "The setup guide is README.md in the Mountain Turtle source folder." }
    }

    func handleURL(_ url: URL) async {
        guard url.scheme == "mountainturtle", url.user == nil, url.password == nil,
              url.port == nil, url.fragment == nil else { return }
        if url.host == "open", url.path.isEmpty || url.path == "/" { return }
        guard url.host == "connection", url.pathComponents.count == 2,
              UUID(uuidString: url.lastPathComponent) != nil,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.queryItems?.count == 1,
              let item = components.queryItems?.first, item.name == "action",
              let action = item.value,
              ["finder", "browse", "settings", "rename", "refresh", "reconnect", "eject", "metrics"].contains(action) else { return }
        // A toolbar URL can arrive during the launch-time status refresh.
        // Resolve it with an awaited snapshot rather than dropping the action.
        do {
            let status = try JSONDecoder().decode(StatusResponse.self, from: await ServiceClient.run(["status"]))
            connections = status.connections
        } catch { self.error = error.localizedDescription; return }
        guard let connection = connections.first(where: { $0.id == url.lastPathComponent }) else { return }
        guard activeAction == nil else { error = "Please wait for the current action to finish."; return }
        selectedID = connection.id
        switch action {
        case "finder": openFinder(connection)
        case "browse":
            if connection.supportsPhotoBrowser { drivePanel = DrivePanel(connection: connection, kind: .photos) }
            else {
                driveMessage = connection.isMounted
                    ? "SFTP photos open in Finder. The smaller-preview photo browser is available for S3 drives."
                    : "Connect this SFTP drive to browse photos in Finder. The smaller-preview photo browser is available for S3 drives."
                openFinder(connection)
            }
        case "metrics": drivePanel = DrivePanel(connection: connection, kind: .metrics)
        case "settings": drivePanel = DrivePanel(connection: connection, kind: .settings)
        case "rename": drivePanel = DrivePanel(connection: connection, kind: .rename)
        case "refresh": if await self.action(["refresh", connection.id]) { driveMessage = "Folder listings refreshed. Reopen the folder in Finder to see changes." }
        case "reconnect": _ = await self.action(["reconnect", connection.id])
        case "eject": _ = await self.action(["disconnect", connection.id])
        default: break
        }
    }

    // Change local settings only after the service confirms a clean ejection.
    func updateDrive(_ connection: Connection, arguments: [String]) async -> Bool {
        guard activeAction == nil else { return false }
        activeAction = "updating"
        driveMessage = nil
        defer { activeAction = nil }
        var restore = false
        var beganDisconnect = false
        func request(_ args: [String]) async throws {
            let response = try JSONDecoder().decode(ActionResponse.self, from: await ServiceClient.run(args))
            guard response.ok else { throw TurtleError(message: response.error ?? "Could not update the drive.") }
        }
        do {
            let initial = try JSONDecoder().decode(StatusResponse.self, from: await ServiceClient.run(["status"]))
            guard let current = initial.connections.first(where: { $0.id == connection.id }) else { throw TurtleError(message: "This connection was removed.") }
            guard !current.isWorking else { throw TurtleError(message: "Wait for this drive to finish connecting or ejecting, then try again.") }
            restore = current.desiredConnected || current.isMounted
            if current.isMounted || current.desiredConnected {
                beganDisconnect = true
                try await request(["disconnect", connection.id])
                var ejected = false
                for attempt in 0..<150 {
                    let status = try JSONDecoder().decode(StatusResponse.self, from: await ServiceClient.run(["status"]))
                    guard let live = status.connections.first(where: { $0.id == connection.id }) else { throw TurtleError(message: "This connection was removed.") }
                    if !live.isMounted && live.state == "disconnected" { ejected = true; break }
                    if attempt > 2 && live.state == "error" { throw TurtleError(message: live.message ?? "The drive could not eject. Close files using it, then try again.") }
                    if attempt > 2, let message = live.message,
                       message.hasPrefix("Drive is busy.") || message.hasPrefix("Waiting for pending uploads") {
                        throw TurtleError(message: message)
                    }
                    try await Task.sleep(nanoseconds: 500_000_000)
                }
                guard ejected else { throw TurtleError(message: "The drive is still ejecting. Wait for it to disconnect, then try again.") }
            }
            try await request(arguments)
            if restore { try await request(["connect", connection.id]) }
            await refresh()
            switch arguments.first {
            case "rename": driveMessage = "Drive renamed. Its remote files and cached copies are unchanged."
            case "clear-cache": driveMessage = "Local cache cleared."
            case "access": driveMessage = "Access setting saved." + (restore ? " The drive is reconnecting." : "")
            default: driveMessage = "Download settings saved."
            }
            return true
        } catch {
            let failure = error.localizedDescription
            // Cancel a pending eject as well as restoring an already ejected drive.
            if restore && beganDisconnect { try? await request(["connect", connection.id]) }
            await refresh()
            self.error = failure
            return false
        }
    }
}

struct BrandIcon: View {
    var size: CGFloat = 54
    var body: some View {
        Group {
            if let image = NSImage(contentsOf: ServiceClient.resources.appendingPathComponent("appIcon.png")) {
                Image(nsImage: image).resizable().interpolation(.high)
            } else {
                Image(systemName: "tortoise.fill").resizable().scaledToFit().padding(size * 0.18).foregroundStyle(moss)
            }
        }.frame(width: size, height: size)
    }
}

struct MainView: View {
    @ObservedObject var model: AppModel
    @State private var showAdd = false
    @State private var showSetup = false
    @State private var editing: Connection?
    @State private var removing: Connection?
    @State private var fileDropTargeted = false
    @AppStorage("setupPanelSeenVersion") private var setupPanelSeenVersion = ""
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Your drives").font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Text("PREVIEW \(appVersion)").font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundStyle(moss.opacity(0.8))
                    Button { showSetup = true } label: {
                        Image(systemName: model.setupNeedsAttention ? "wrench.and.screwdriver.fill" : "checkmark.seal")
                    }.buttonStyle(.plain).help("Setup checklist").padding(.leading, 10)
                    Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.plain).help("Refresh connection status").padding(.leading, 10)
                }.padding(.horizontal, 32).padding(.vertical, 24)
                if let message = model.serviceError {
                    notice(message, symbol: "exclamationmark.triangle", color: .orange).padding(.horizontal, 32).padding(.bottom, 16)
                }
                if model.missingTools && model.serviceError == nil {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: "shippingbox").foregroundStyle(moss)
                        VStack(alignment: .leading, spacing: 5) {
                            Text("A little setup first").fontWeight(.semibold)
                            Text("Install rclone to connect drives. S3 connections also use AWS CLI.").foregroundStyle(.secondary)
                            Button("Open setup") { showSetup = true }.buttonStyle(.link)
                        }
                    }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(cream).clipShape(RoundedRectangle(cornerRadius: 14)).padding(.horizontal, 32)
                }
                if let connection = model.selected { detail(connection) }
                else { welcome }
            }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 900, minHeight: 610)
        .tint(moss)
        .dropDestination(for: URL.self) { urls, _ in
            model.receiveConnectionFiles(urls)
            return true
        } isTargeted: { fileDropTargeted = $0 }
        .overlay {
            if fileDropTargeted {
                ZStack {
                    RoundedRectangle(cornerRadius: 14).fill(cream.opacity(0.95))
                    RoundedRectangle(cornerRadius: 14).strokeBorder(moss, style: StrokeStyle(lineWidth: 3, dash: [10]))
                    Label("Drop to review connection", systemImage: "square.and.arrow.down")
                        .font(.title2.weight(.semibold)).foregroundStyle(moss)
                }.padding(12).allowsHitTesting(false)
            }
        }
        .sheet(isPresented: $showAdd) { ConnectionEditor(model: model, original: nil) }
        .sheet(isPresented: $showSetup) { SetupView(model: model) }
        .sheet(item: $editing) { ConnectionEditor(model: model, original: $0) }
        .sheet(item: $model.transferRequest) { request in
            switch request.kind {
            case .export(let connection): ConnectionExportView(model: model, connection: connection)
            case .importFile(let data): ConnectionImportView(model: model, data: data)
            }
        }
        .sheet(item: $model.drivePanel) { panel in
            switch panel.kind {
            case .photos: PhotoBrowserView(connection: panel.connection)
            case .metrics: DriveMetricsView(connection: panel.connection)
            case .settings: DriveSettingsView(model: model, connection: panel.connection)
            case .rename: RenameDriveView(model: model, connection: panel.connection)
            case .access: DriveAccessView(model: model, connection: panel.connection)
            }
        }
        .alert("Couldn’t finish that action", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("OK") { model.error = nil }
        } message: { Text(model.error ?? "") }
        .alert("Remove this saved connection?", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("Cancel", role: .cancel) { removing = nil }
            Button("Remove connection", role: .destructive) {
                if let connection = removing { Task { await model.action(["remove", connection.id]) } }
                removing = nil
            }
        } message: { Text("This removes the connection from Mountain Turtle. Remote files stay where they are.") }
        .onReceive(NotificationCenter.default.publisher(for: .showTurtleWindow)) { _ in openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
        .onChange(of: model.setupNeedsAttention) { _, needsAttention in
            guard needsAttention, setupPanelSeenVersion != appVersion,
                  model.transferRequest == nil, model.activeAction == nil else { return }
            setupPanelSeenVersion = appVersion
            showSetup = true
        }
        .onChange(of: showAdd || showSetup || editing != nil || model.drivePanel != nil || removing != nil) { _, presented in
            model.otherSheetPresented = presented
        }
        .onChange(of: model.selectedID) { _, _ in
            model.driveMessage = nil
            model.loginMessage = nil
        }
        .onAppear { model.start() }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 9) {
                BrandIcon(size: 48)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Mountain").font(.system(size: 16, weight: .semibold))
                    Text("Turtle").font(.system(size: 16, weight: .semibold))
                }.foregroundStyle(ink)
            }.padding(.horizontal, 20).padding(.top, 26).padding(.bottom, 30)
            HStack {
                Text("CONNECTIONS").font(.system(size: 10, weight: .semibold)).tracking(1.3).foregroundStyle(.secondary)
                Spacer()
                Text("\(model.connections.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }.padding(.horizontal, 24).padding(.bottom, 10)
            ScrollView {
                VStack(spacing: 5) {
                    ForEach(model.connections) { connection in
                        Button { model.selectedID = connection.id } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "externaldrive.fill").font(.system(size: 19)).foregroundStyle(connection.isConnected ? moss : .secondary)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(connection.name).font(.system(size: 13, weight: .semibold)).lineLimit(2)
                                    HStack(spacing: 5) {
                                        Circle().fill(connection.color).frame(width: 5, height: 5)
                                        Text(connection.title).font(.system(size: 11)).foregroundStyle(.secondary)
                                    }
                                }
                                Spacer(minLength: 0)
                            }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                                .background(model.selectedID == connection.id ? Color.white.opacity(0.92) : .clear)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).accessibilityLabel("\(connection.name), \(connection.title)")
                            .contextMenu {
                                Button("Export connection…") { model.exportConnection(connection) }
                                    .disabled(model.activeAction != nil || model.transferRequest != nil)
                            }
                    }
                }.padding(.horizontal, 12)
            }
            VStack(spacing: 10) {
                Button { showAdd = true } label: {
                    Label("Add connection", systemImage: "plus").frame(maxWidth: .infinity)
                }.controlSize(.large)
                Button { model.chooseConnectionFile() } label: {
                    Label("Import connection…", systemImage: "square.and.arrow.down")
                }.buttonStyle(.link).disabled(model.activeAction != nil)
            }.padding(.horizontal, 20).padding(.vertical, 18)
            Divider().padding(.horizontal, 20)
            VStack(alignment: .leading, spacing: 14) {
                UpdateSettingsView()
                Toggle("Restore drives at login", isOn: Binding(get: { model.launchAtLogin }, set: { value in Task { await model.action(["autostart", value ? "on" : "off"]) } }))
                    .toggleStyle(.checkbox).font(.system(size: 11)).disabled(model.activeAction != nil)
                Button { FIFinderSyncController.showExtensionManagementInterface() } label: {
                    Label(model.finderBadgesEnabled ? "Finder badges enabled" : "Enable Finder badges…",
                          systemImage: model.finderBadgesEnabled ? "checkmark.seal" : "externaldrive.badge.checkmark")
                        .font(.system(size: 11))
                }.buttonStyle(.link).help("Show cloud and local-cache status on files in Finder")
                HStack(spacing: 5) {
                    Image(systemName: "heart").font(.system(size: 10))
                    Text("Free & open source").font(.system(size: 10))
                    Spacer()
                    Button { model.openGuide() } label: { Image(systemName: "questionmark.circle") }.buttonStyle(.plain).help("Setup and help")
                }.foregroundStyle(moss.opacity(0.85))
            }.padding(20)
        }.frame(width: 244).background(cream)
    }

    private var welcome: some View {
        VStack(spacing: 20) {
            Spacer()
            BrandIcon(size: 138)
            Text("Your files. On your Mac.").font(.system(size: 28, weight: .semibold)).foregroundStyle(ink)
            Text("Connect an S3 bucket or SFTP server in Finder.\nDownload and cache files as you need them.")
                .font(.system(size: 14)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(5)
            Button("Add your first connection") { showAdd = true }.buttonStyle(.borderedProminent).controlSize(.large).padding(.top, 6)
            Text("Or open a .turtle file to add a connection.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            HStack(spacing: 24) {
                Label("S3 & SFTP", systemImage: "network")
                Label("Finder drives", systemImage: "externaldrive")
                Label("No subscription", systemImage: "checkmark.seal")
            }.font(.system(size: 11)).foregroundStyle(moss).padding(.top, 16)
            Spacer()
            Text("Your files stay on your connected storage.").font(.system(size: 11)).foregroundStyle(.secondary).padding(.bottom, 30)
        }.frame(maxWidth: .infinity)
    }

    private func detail(_ connection: Connection) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .top, spacing: 18) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 20).fill(moss.opacity(0.08)).frame(width: 86, height: 86)
                        Image(systemName: "externaldrive.fill").font(.system(size: 43)).foregroundStyle(moss)
                        Text(connection.isSFTP ? "SFTP" : "S3").font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(cream).offset(y: -6)
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        Text(connection.name).font(.system(size: 28, weight: .semibold)).foregroundStyle(ink).textSelection(.enabled)
                        HStack(spacing: 7) {
                            if connection.isWorking { ProgressView().controlSize(.small) }
                            else { Circle().fill(connection.color).frame(width: 7, height: 7) }
                            Text(connection.title).font(.system(size: 13, weight: .medium)).foregroundStyle(connection.color)
                        }
                    }.padding(.top, 5)
                    Spacer()
                    Menu {
                        if connection.supportsPhotoBrowser {
                            Button("Browse photos…") { model.drivePanel = DrivePanel(connection: connection, kind: .photos) }
                        }
                        Button("Drive insights…") { model.drivePanel = DrivePanel(connection: connection, kind: .metrics) }
                        Button("Download & cache settings…") { model.drivePanel = DrivePanel(connection: connection, kind: .settings) }
                        Button("Change access…") { model.drivePanel = DrivePanel(connection: connection, kind: .access) }
                            .disabled(connection.isWorking || model.activeAction != nil)
                        Button("Rename drive…") { model.drivePanel = DrivePanel(connection: connection, kind: .rename) }
                        Divider()
                        Button("Refresh folder listings") { Task { await model.action(["refresh", connection.id]) } }.disabled(!connection.isConnected)
                        Button("Reconnect drive") { Task { await model.action(["reconnect", connection.id]) } }
                        Divider()
                        Button("Edit connection…") { editing = connection }.disabled(connection.isMounted || connection.desiredConnected || connection.isWorking)
                        Button("Export connection…") { model.exportConnection(connection) }.disabled(model.activeAction != nil)
                        Button("Remove connection…", role: .destructive) { removing = connection }.disabled(connection.isMounted || connection.desiredConnected || connection.isWorking)
                    } label: { Image(systemName: "ellipsis.circle").font(.system(size: 20)) }.menuStyle(.borderlessButton).frame(width: 25)
                }
                if let message = connection.message, !message.isEmpty {
                    notice(message, symbol: connection.state == "error" || connection.state == "needsLogin" ? "exclamationmark.circle" : "info.circle", color: connection.state == "error" || connection.state == "needsLogin" ? .orange : moss)
                }
                if let message = connection.sidebarError, !message.isEmpty {
                    notice(message, symbol: "sidebar.left", color: .orange)
                }
                HStack(spacing: 10) {
                    if connection.isConnected {
                        Button { model.openFinder(connection) } label: { Label("Show in Finder", systemImage: "folder") }.buttonStyle(.borderedProminent)
                        Button { Task { await model.action(["disconnect", connection.id]) } } label: { Label("Eject", systemImage: "eject") }
                        if !connection.isSFTP { Button("Sign in to AWS") { Task { await model.action(["login", connection.id]) } } }
                    } else if connection.state == "needsLogin" && !connection.isSFTP {
                        Button { Task { await model.action(["login", connection.id]) } } label: { Label("Sign in to AWS", systemImage: "person.badge.key") }.buttonStyle(.borderedProminent)
                        Button("Try connecting again") { Task { await model.action(["connect", connection.id]) } }
                    } else {
                        Button { Task { await model.action(["connect", connection.id]) } } label: { Label(connection.state == "disconnecting" ? "Ejecting…" : connection.isWorking ? "Connecting…" : "Connect drive", systemImage: "bolt.horizontal") }.buttonStyle(.borderedProminent).disabled(connection.isWorking)
                        if !connection.isSFTP { Button("Sign in to AWS") { Task { await model.action(["login", connection.id]) } }.disabled(connection.isWorking) }
                    }
                    if !connection.isConnected && (connection.isMounted || connection.desiredConnected) {
                        Button(connection.isMounted ? "Eject" : "Cancel connection") { Task { await model.action(["disconnect", connection.id]) } }
                            .disabled(connection.state == "disconnecting")
                    }
                }.controlSize(.large).disabled(model.activeAction != nil)
                if model.activeAction == "login" && !connection.isSFTP {
                    HStack(spacing: 10) {
                        ProgressView().controlSize(.small)
                        Text("Approve the request on the AWS page within five minutes. Mountain Turtle will confirm when sign-in finishes.").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                if let message = model.loginMessage, !connection.isSFTP {
                    notice(message, symbol: "checkmark.circle", color: moss)
                }
                if let message = model.driveMessage {
                    notice(message, symbol: "checkmark.circle", color: moss)
                }
                if connection.supportsPhotoBrowser {
                HStack(spacing: 12) {
                    Image(systemName: "photo.on.rectangle.angled").font(.title2).foregroundStyle(moss)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Browse with smaller previews").font(.system(size: 13, weight: .semibold))
                        Text("Preview visible photos. Choose which originals to keep.")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Browse photos") { model.drivePanel = DrivePanel(connection: connection, kind: .photos) }
                }.padding(16).background(cream).clipShape(RoundedRectangle(cornerRadius: 14))
                }
                HStack(spacing: 12) {
                    Image(systemName: "chart.xyaxis.line").font(.title2).foregroundStyle(moss)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Drive insights").font(.system(size: 13, weight: .semibold))
                        Text(connection.isSFTP ? "Track transfers, local cache and remote server capacity." : "See bucket activity, storage, AWS prices and actual spend.")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("View metrics") { model.drivePanel = DrivePanel(connection: connection, kind: .metrics) }
                }.padding(16).background(cream).clipShape(RoundedRectangle(cornerRadius: 14))
                Button { model.drivePanel = DrivePanel(connection: connection, kind: .settings) } label: {
                    Label("Download & cache settings", systemImage: "slider.horizontal.3")
                }.buttonStyle(.link)
                VStack(spacing: 0) {
                    if connection.isSFTP {
                        infoRow("Server", "\(connection.host ?? ""):\(connection.port ?? 22)", symbol: "server.rack")
                        Divider().padding(.leading, 42)
                        infoRow("Username", connection.user ?? "", symbol: "person.crop.circle")
                        Divider().padding(.leading, 42)
                        infoRow("Folder", connection.remotePath.flatMap { $0.isEmpty ? nil : $0 } ?? "Home folder", symbol: "folder")
                        Divider().padding(.leading, 42)
                        infoRow("Sign-in", connection.authenticationLabel, symbol: "key")
                    } else {
                        infoRow("S3 bucket", connection.bucket, symbol: "shippingbox")
                        Divider().padding(.leading, 42)
                        S3DetailsRows(connection: connection)
                            .id([connection.id, connection.bucket, connection.profile, connection.region])
                        Divider().padding(.leading, 42)
                        infoRow("AWS profile", connection.profile, symbol: "person.crop.circle")
                        Divider().padding(.leading, 42)
                        infoRow("Region", connection.region, symbol: "globe.americas")
                    }
                    Divider().padding(.leading, 42)
                    HStack(spacing: 12) {
                        infoRow("Access", connection.readOnly ? "Read only" : "Read & write", symbol: connection.readOnly ? "lock" : "pencil")
                        Button("Change…") { model.drivePanel = DrivePanel(connection: connection, kind: .access) }
                            .buttonStyle(.link).font(.system(size: 12))
                            .disabled(connection.isWorking || model.activeAction != nil)
                            .accessibilityLabel("Change drive access")
                    }
                    Divider().padding(.leading, 42)
                    infoRow("At login", connection.autoConnect ? "Reconnect this drive" : "Connect manually", symbol: "arrow.clockwise")
                }.padding(.horizontal, 16).background(cream.opacity(0.85)).clipShape(RoundedRectangle(cornerRadius: 16))
                HStack(alignment: .top, spacing: 11) {
                    Image(systemName: "leaf").font(.system(size: 19)).foregroundStyle(moss)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Download only as needed").font(.system(size: 13, weight: .semibold))
                        Text(connection.isSFTP ? "Finder previews can read original photos. Turn off icon previews in Finder’s View Options to reduce downloads." : "Finder previews can read original photos. Use Browse photos to avoid those full downloads, or turn off icon previews in Finder’s View Options. AWS may occasionally ask you to sign in again.")
                            .font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
                    }
                }.padding(.top, 2)
            }.padding(.horizontal, 32).padding(.bottom, 30)
        }
    }

    private func infoRow(_ label: String, _ value: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 17).foregroundStyle(moss.opacity(0.8))
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(value).frame(maxWidth: .infinity, alignment: .trailing).textSelection(.enabled).lineLimit(2)
        }.font(.system(size: 12)).padding(.vertical, 13)
    }

    private func notice(_ text: String, symbol: String, color: Color) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(color)
            Text(text).font(.system(size: 12)).lineSpacing(3).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.padding(14).background(color.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius: 12))
    }
}

private enum SetupState {
    case ready, waiting, problem

    var symbol: String {
        switch self {
        case .ready: return "checkmark.circle.fill"
        case .waiting: return "clock"
        case .problem: return "exclamationmark.circle.fill"
        }
    }

    var color: Color {
        switch self {
        case .ready: return moss
        case .waiting: return .secondary
        case .problem: return .orange
        }
    }
}

struct SetupView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var installing = false
    @State private var installLog = ""
    @State private var includeAWS = false
    @State private var message: String?

    private var needsToolInstall: Bool {
        (includeAWS && !model.dependencies.awsReady) || !model.dependencies.rcloneReady
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 13) {
                BrandIcon(size: 52)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Mac setup").font(.system(size: 23, weight: .semibold))
                    Text("Mountain Turtle checks the tools it needs before connecting drives.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            VStack(spacing: 0) {
                setupRow("Mountain Turtle in Applications",
                         detail: model.dependencies.installedReady ? (model.dependencies.appPath ?? "Installed") : "Move the app into your Applications folder before using login restore.",
                         state: model.dependencies.installedReady ? .ready : .problem)
                Divider().padding(.leading, 42)
                setupRow("AWS CLI v2",
                         detail: model.dependencies.awsReady ? (model.dependencies.awsVersion ?? "Installed") : "Only needed for S3 drives. SFTP works without AWS CLI.",
                         state: model.dependencies.awsReady ? .ready : model.requiresAWS ? .problem : .waiting)
                Divider().padding(.leading, 42)
                setupRow("rclone NFS mounts",
                         detail: model.dependencies.rcloneReady ? (model.dependencies.rcloneVersion ?? "Installed") : "Needed to show S3 and SFTP as macOS network volumes.",
                         state: model.dependencies.rcloneReady ? .ready : .problem)
                Divider().padding(.leading, 42)
                setupRow("Finder Network Volumes",
                         detail: model.dependencies.privacyMessage ?? "macOS asks the first time a drive is added to Finder.",
                         state: privacyState)
            }.padding(.horizontal, 16).background(cream.opacity(0.85)).clipShape(RoundedRectangle(cornerRadius: 16))
            if !model.dependencies.awsReady {
                Toggle("Include AWS CLI for S3 connections", isOn: $includeAWS)
                    .toggleStyle(.checkbox).font(.callout).disabled(installing)
            }
            HStack(spacing: 10) {
                if !model.dependencies.installedReady {
                    Button { installApp() } label: { Label("Move to Applications", systemImage: "square.and.arrow.down") }
                }
                if needsToolInstall {
                    Button { installTools() } label: {
                        Label(model.dependencies.brew == nil ? "Install Homebrew & tools" : "Install tools", systemImage: "shippingbox")
                    }.buttonStyle(.borderedProminent).disabled(installing)
                }
                Button { ServiceClient.openPrivacySettings() } label: { Label("Open Privacy Settings", systemImage: "lock.shield") }
                Spacer()
                Button { Task { await model.refresh() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
            }.controlSize(.large)
            if installing {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text("Installing with Homebrew...")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            if let message {
                Label(message, systemImage: "info.circle").font(.system(size: 12)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !installLog.isEmpty {
                ScrollView {
                    Text(installLog).font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 140).padding(10).background(Color(nsColor: .textBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            HStack {
                Button("Setup guide") { model.openGuide() }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }.padding(28).frame(width: 640).tint(moss)
            .onAppear { includeAWS = model.requiresAWS }
    }

    private var privacyState: SetupState {
        switch model.dependencies.privacyState {
        case "approved": return .ready
        case "needsApproval": return .problem
        default: return .waiting
        }
    }

    private func setupRow(_ title: String, detail: String, state: SetupState) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: state.symbol).frame(width: 18).foregroundStyle(state.color)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail).font(.system(size: 12)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            Spacer()
        }.padding(.vertical, 13)
    }

    private func installApp() {
        do {
            let destination = try ServiceClient.installCurrentAppToApplications()
            message = "Mountain Turtle was copied to Applications. The installed app will open now."
            NSWorkspace.shared.open(destination)
            NSApp.terminate(nil)
        } catch {
            message = error.localizedDescription
        }
    }

    private func installTools() {
        message = nil
        installLog = ""
        if model.dependencies.brew == nil {
            do {
                try ServiceClient.openTerminalInstaller(includeAWS: includeAWS)
                message = "The installer is open in Terminal because Homebrew may need your Mac password. Return here and refresh when it finishes."
            } catch {
                message = error.localizedDescription
            }
            return
        }
        installing = true
        Task {
            do {
                try await ServiceClient.installToolsWithHomebrew(includeAWS: includeAWS) { text in
                    Task { @MainActor in
                        installLog += text
                        if installLog.count > 8000 { installLog = String(installLog.suffix(8000)) }
                    }
                }
                await model.refresh()
                message = includeAWS ? "AWS CLI and rclone are installed." : "rclone is installed. SFTP connections are ready to set up."
            } catch {
                message = error.localizedDescription
            }
            installing = false
        }
    }
}

struct ConnectionEditor: View {
    @ObservedObject var model: AppModel
    var original: Connection?
    var imported: ConnectionImportDraft? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var backend = "s3"
    @State private var bucket = ""
    @State private var profile = ""
    @State private var region = "us-east-1"
    @State private var host = ""
    @State private var user = ""
    @State private var port = "22"
    @State private var remotePath = ""
    @State private var authMode = "agent"
    @State private var keyFile = ""
    @State private var knownHostsFile = "~/.ssh/known_hosts"
    @State private var password = ""
    @State private var readOnly = true
    @State private var autoConnect = false
    @State private var saving = false
    @State private var failure: String?

    private var isSFTP: Bool { backend == "sftp" }
    private func trimmed(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var portIsValid: Bool { Int(port).map { (1...65535).contains($0) } ?? false }
    private var keepsPassword: Bool {
        original?.isSFTP == true && original?.passwordConfigured == true
            && trimmed(host) == original?.host && trimmed(user) == original?.user
            && Int(port) == (original?.port ?? 22)
    }
    private var valid: Bool {
        guard !trimmed(name).isEmpty else { return false }
        if !isSFTP { return !trimmed(bucket).isEmpty && !trimmed(profile).isEmpty && !trimmed(region).isEmpty }
        guard !trimmed(host).isEmpty, !trimmed(user).isEmpty, portIsValid, !trimmed(knownHostsFile).isEmpty else { return false }
        if authMode == "keyFile" && trimmed(keyFile).isEmpty { return false }
        if authMode == "password" {
            return (!password.isEmpty || keepsPassword) && !password.contains("\n") && !password.contains("\r")
        }
        return true
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 13) {
                BrandIcon(size: 52)
                VStack(alignment: .leading, spacing: 5) {
                    Text(imported != nil ? "Review imported connection" : original == nil ? "Add a drive" : "Edit connection").font(.system(size: 23, weight: .semibold))
                    Text(isSFTP ? "Connect securely to a server over SSH." : "A saved AWS profile keeps your keys out of the app.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if imported != nil {
                        Text(importNotice).font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let imported {
                            Text("Cache: \(imported.connection.cacheMaxSizeMiB ?? 2048) MiB, \(imported.connection.cacheMaxAgeHours ?? 24) hours. You can change this in Download & cache settings.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Form {
                        Picker("Connection type", selection: $backend) {
                            Text("Amazon S3").tag("s3")
                            Text("SFTP server").tag("sftp")
                        }.disabled(original != nil || imported != nil)
                        TextField("Drive name", text: $name, prompt: Text("e.g. My files"))
                        if isSFTP { sftpFields } else { s3Fields }
                    }.textFieldStyle(.roundedBorder).font(.system(size: 13))
                    if isSFTP {
                        VStack(alignment: .leading, spacing: 6) {
                            Label("Verify the server first", systemImage: "lock.shield").fontWeight(.medium)
                            Text("Connect with SSH and verify the server fingerprint with its administrator first. Mountain Turtle requires its trusted key in your known hosts file; unknown or changed keys are rejected.")
                            if authMode == "agent" {
                                Text("Load your SSH key into the macOS SSH agent before connecting. An unlocked agent key is required for automatic reconnects.")
                            } else if authMode == "keyFile" {
                                Text("For a key protected by a passphrase, load it into your SSH agent and choose SSH agent above.")
                            } else {
                                Text(keepsPassword ? "Leave the password blank to keep it for this server and account. Changing the server, username, or port requires a new password." : "Enter a password for this server and account. It will be stored in macOS Keychain.")
                            }
                        }.font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    } else if !model.dependencies.awsReady {
                        Text("S3 connections need AWS CLI v2. Install it from Mac setup.").font(.caption).foregroundStyle(.secondary)
                    }
                    VStack(alignment: .leading, spacing: 13) {
                        Toggle("Read only", isOn: $readOnly).toggleStyle(.checkbox)
                        Text(readOnly ? "Browse and download. Remote changes are disabled." : "Saving, moving, or deleting files in Finder changes the remote files.")
                            .font(.system(size: 11)).foregroundStyle(.secondary).padding(.leading, 20)
                        Toggle("Reconnect this drive at login", isOn: $autoConnect).toggleStyle(.checkbox)
                        Text("Also enable “Restore drives at login” in the main window.").font(.system(size: 11)).foregroundStyle(.secondary).padding(.leading, 20)
                    }.font(.system(size: 12)).padding(16).frame(maxWidth: .infinity, alignment: .leading).background(cream).clipShape(RoundedRectangle(cornerRadius: 12))
                }
            }.frame(maxHeight: isSFTP ? 500 : 280)
            if let failure {
                Label(failure, systemImage: "exclamationmark.circle").font(.system(size: 12)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Cancel", role: .cancel) { password = ""; dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(imported != nil ? "Import connection" : original == nil ? "Add connection" : "Save changes") { save() }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(!valid || saving)
            }
        }.padding(28).frame(width: 550).tint(moss)
            .interactiveDismissDisabled(saving)
            .onAppear {
                guard let source = original ?? imported?.connection else { return }
                name = source.name; backend = source.backend ?? "s3"
                bucket = source.bucket; profile = source.profile; region = source.region
                host = source.host ?? ""; user = source.user ?? ""; port = String(source.port ?? 22)
                remotePath = source.remotePath ?? ""; authMode = source.authMode ?? "agent"
                keyFile = source.keyFile ?? ""; knownHostsFile = source.knownHostsFile ?? "~/.ssh/known_hosts"
                readOnly = source.readOnly; autoConnect = original?.autoConnect ?? false
                password = imported?.password ?? ""
            }
            .onChange(of: [host, user, port]) { _, _ in
                if let source = imported?.connection,
                   trimmed(host) != source.host || trimmed(user) != source.user || Int(port) != source.port {
                    password = ""
                }
            }
    }

    private var importNotice: String {
        let start = "Review these settings before saving a new connection. It will stay disconnected until you choose Connect drive. "
        if !isSFTP { return start + "Choose an AWS profile configured on this Mac; AWS credentials are not included." }
        if imported?.password != nil { return start + "The included SFTP password will be saved in this Mac’s Keychain. Verify the server and choose a local known hosts file." }
        return start + "Set up authentication and the trusted server key on this Mac. Passwords and SSH key files are not included."
    }

    @ViewBuilder private var s3Fields: some View {
        TextField("S3 bucket", text: $bucket, prompt: Text("your-bucket-name"))
        HStack {
            TextField("AWS profile", text: $profile, prompt: Text("Choose a saved profile"))
            Menu { ForEach(model.profiles, id: \.self) { p in Button(p) { profile = p } } } label: { Image(systemName: "chevron.down") }
                .menuStyle(.borderlessButton).frame(width: 20).disabled(model.profiles.isEmpty)
        }
        TextField("AWS region", text: $region)
    }

    @ViewBuilder private var sftpFields: some View {
        TextField("Server", text: $host, prompt: Text("sftp.example.com"))
        TextField("Username", text: $user, prompt: Text("Your server username"))
        TextField("Port", text: $port)
        if !portIsValid { Text("Enter a port from 1 to 65535.").font(.caption).foregroundStyle(.red) }
        TextField("Remote folder", text: $remotePath, prompt: Text("Leave blank for your home folder"))
        Text("Use an absolute path or a path relative to your server home folder.")
            .font(.caption).foregroundStyle(.secondary)
        Picker("Authentication", selection: $authMode) {
            Text("SSH agent").tag("agent")
            Text("Private key file").tag("keyFile")
            Text("Password").tag("password")
        }
        if authMode == "keyFile" { fileField("Private key", path: $keyFile) }
        if authMode == "password" {
            SecureField("Password", text: $password, prompt: Text(keepsPassword ? "Saved in Keychain; leave blank to keep" : "Your server password"))
        }
        fileField("Known hosts", path: $knownHostsFile)
    }

    private func fileField(_ title: String, path: Binding<String>) -> some View {
        HStack {
            TextField(title, text: path)
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.canChooseFiles = true; panel.canChooseDirectories = false
                panel.allowsMultipleSelection = false; panel.showsHiddenFiles = true
                panel.title = "Choose \(title.lowercased()) file"
                let expanded = (path.wrappedValue as NSString).expandingTildeInPath
                panel.directoryURL = expanded.isEmpty ? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ssh") : URL(fileURLWithPath: expanded).deletingLastPathComponent()
                if panel.runModal() == .OK, let url = panel.url { path.wrappedValue = url.path }
            }.accessibilityLabel("Choose \(title.lowercased()) file")
        }
    }

    private func save() {
        saving = true; failure = nil
        var args = original.map { ["edit", $0.id] } ?? ["add"]
        args += ["--name", trimmed(name), "--backend", backend]
        var input: Data?
        if isSFTP {
            args += ["--host", trimmed(host), "--user", trimmed(user), "--port", port, "--remote-path", remotePath,
                     "--auth-mode", authMode, "--key-file", trimmed(keyFile), "--known-hosts-file", trimmed(knownHostsFile)]
            if authMode == "password", !password.isEmpty {
                args.append("--password-stdin")
                input = Data(password.utf8)
            }
        } else { args += ["--bucket", trimmed(bucket), "--profile", trimmed(profile), "--region", trimmed(region)] }
        args.append(readOnly ? "--read-only" : "--read-write")
        if let imported {
            args += ["--cache-max-size-mib", String(imported.connection.cacheMaxSizeMiB ?? 2048),
                     "--cache-max-age-hours", String(imported.connection.cacheMaxAgeHours ?? 24)]
        }
        if autoConnect { args.append("--auto-connect") }
        Task {
            if await model.action(args, standardInput: input) {
                password = ""
                if imported != nil { model.driveMessage = "Connection imported. Choose Connect drive when you are ready." }
                dismiss()
            }
            else { failure = model.error; model.error = nil }
            saving = false
        }
    }
}

extension Notification.Name { static let showTurtleWindow = Notification.Name("showTurtleWindow") }

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem?
    private var observation: AnyCancellable?
    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item?.button?.image = NSImage(systemSymbolName: "tortoise.fill", accessibilityDescription: "Mountain Turtle")
        item?.button?.toolTip = "Mountain Turtle"
        observation = AppModel.shared.$connections.sink { [weak self] _ in DispatchQueue.main.async { self?.updateMenu() } }
        updateMenu()
        AppModel.shared.start()
        AppUpdater.shared.start()
    }
    private func updateMenu() {
        let menu = NSMenu()
        let title = NSMenuItem(title: "Mountain Turtle", action: #selector(showWindow), keyEquivalent: "")
        title.target = self; menu.addItem(title); menu.addItem(.separator())
        for connection in AppModel.shared.connections {
            let row = NSMenuItem(title: "\(connection.name) · \(connection.title)", action: #selector(selectConnection(_:)), keyEquivalent: "")
            row.target = self; row.representedObject = connection.id
            row.image = NSImage(systemSymbolName: connection.isConnected ? "externaldrive.fill.badge.checkmark" : "externaldrive", accessibilityDescription: connection.title)
            menu.addItem(row)
        }
        if AppModel.shared.connections.isEmpty { let empty = NSMenuItem(title: "No saved connections", action: nil, keyEquivalent: ""); empty.isEnabled = false; menu.addItem(empty) }
        menu.addItem(.separator())
        let show = NSMenuItem(title: "Show connections…", action: #selector(showWindow), keyEquivalent: ""); show.target = self; menu.addItem(show)
        let updates = NSMenuItem(title: "Check for Updates…", action: #selector(AppUpdater.checkForUpdates(_:)), keyEquivalent: "")
        updates.target = AppUpdater.shared; menu.addItem(updates)
        let quit = NSMenuItem(title: "Quit app (keep drives connected)", action: #selector(quitApp), keyEquivalent: "q"); quit.target = self; menu.addItem(quit)
        item?.menu = menu
    }
    @objc private func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        if let window = NSApp.windows.first(where: { $0.canBecomeMain && !($0 is NSPanel) }) { window.makeKeyAndOrderFront(nil) }
        NotificationCenter.default.post(name: .showTurtleWindow, object: nil)
    }
    @objc private func selectConnection(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        AppModel.shared.selectedID = id
        if let connection = AppModel.shared.selected, connection.isConnected { AppModel.shared.openFinder(connection) }
        else { showWindow() }
    }
    @objc private func quitApp() { NSApp.terminate(nil) }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppUpdater.shared.terminationReply(for: sender)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func application(_ application: NSApplication, open urls: [URL]) {
        if urls.contains(where: \.isFileURL) {
            showWindow()
            AppModel.shared.receiveConnectionFiles(urls)
            return
        }
        Logger(subsystem: "io.mountainturtle.app", category: "actions").notice("Received a Finder URL action.")
        guard let url = urls.first, url.scheme == "mountainturtle" else { return }
        showWindow()
        Task { await AppModel.shared.handleURL(url) }
    }
}

@main struct MountainTurtleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = AppModel.shared
    var body: some Scene {
        Window("Mountain Turtle", id: "main") { MainView(model: model).preferredColorScheme(.light) }
            .defaultSize(width: 970, height: 680)
            .windowResizability(.contentMinSize)
            .commands {
                CommandGroup(after: .appInfo) { CheckForUpdatesButton() }
                CommandGroup(replacing: .newItem) {
                    Button("Show connections") { NotificationCenter.default.post(name: .showTurtleWindow, object: nil) }.keyboardShortcut("n")
                    Button("Import connection…") {
                        NotificationCenter.default.post(name: .showTurtleWindow, object: nil)
                        model.chooseConnectionFile()
                    }.keyboardShortcut("o").disabled(model.activeAction != nil || model.transferRequest != nil || model.otherSheetPresented)
                    Button("Export connection…") {
                        if let connection = model.selected { model.exportConnection(connection) }
                    }.keyboardShortcut("e", modifiers: [.command, .shift])
                        .disabled(model.selected == nil || model.activeAction != nil || model.transferRequest != nil || model.otherSheetPresented)
                }
                CommandGroup(replacing: .help) { Button("Mountain Turtle Help") { model.openGuide() } }
            }
    }
}
