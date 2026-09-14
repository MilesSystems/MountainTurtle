import SwiftUI
import AppKit
import Combine
import FinderSync
import os

let moss = Color(red: 0.18, green: 0.37, blue: 0.29)
let cream = Color(red: 0.97, green: 0.97, blue: 0.94)
let ink = Color(red: 0.13, green: 0.20, blue: 0.17)

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

    static func run(_ arguments: [String]) async throws -> Data {
        let resources = self.resources
        let script = resources.appendingPathComponent("service/turtle_service.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw TurtleError(message: "The mount service is missing from this app. Rebuild Mountain Turtle with the included build script.")
        }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let output = Pipe(), errors = Pipe()
                process.standardOutput = output
                process.standardError = errors
                do {
                    guard let python = pythonPath else { throw TurtleError(message: "A working Python 3.9 or newer is needed to run the mount service. See the setup guide.") }
                    process.executableURL = URL(fileURLWithPath: python)
                    process.arguments = [script.path, "--resource-dir", resources.path] + arguments
                    try process.run()
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
    private var refreshing = false
    private var timer: Timer?

    var selected: Connection? { connections.first { $0.id == selectedID } }
    var connectedCount: Int { connections.filter(\.isConnected).count }
    var missingTools: Bool { !isLoading && (dependencies.aws == nil || dependencies.rclone == nil) }

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

    @discardableResult func action(_ args: [String]) async -> Bool {
        guard activeAction == nil else { return false }
        activeAction = args.first
        loginMessage = nil
        defer { activeAction = nil }
        do {
            let data = try await ServiceClient.run(args)
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
              ["finder", "browse", "settings", "rename", "refresh", "reconnect", "eject"].contains(action) else { return }
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
        case "browse": drivePanel = DrivePanel(connection: connection, kind: .photos)
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
            restore = current.desiredConnected
            if current.isMounted || current.desiredConnected {
                try await request(["disconnect", connection.id])
                beganDisconnect = true
                var ejected = false
                for attempt in 0..<150 {
                    let status = try JSONDecoder().decode(StatusResponse.self, from: await ServiceClient.run(["status"]))
                    guard let live = status.connections.first(where: { $0.id == connection.id }) else { throw TurtleError(message: "This connection was removed.") }
                    if !live.isMounted && live.state == "disconnected" { ejected = true; break }
                    if attempt > 2 && live.state == "error" { throw TurtleError(message: live.message ?? "The drive could not eject. Close files using it, then try again.") }
                    try await Task.sleep(nanoseconds: 500_000_000)
                }
                guard ejected else { throw TurtleError(message: "The drive is still ejecting. Wait for it to disconnect, then try again.") }
            }
            try await request(arguments)
            if restore { try await request(["connect", connection.id]) }
            await refresh()
            driveMessage = arguments.first == "rename" ? "Drive renamed. Its S3 bucket and cached files are unchanged." : arguments.first == "clear-cache" ? "Local cache cleared." : "Download settings saved."
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
    @State private var editing: Connection?
    @State private var removing: Connection?
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Your S3 drives").font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Text("PREVIEW 0.2").font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundStyle(moss.opacity(0.8))
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
                            Text("Install AWS CLI and rclone to connect your S3 buckets.").foregroundStyle(.secondary)
                            Button("Open setup guide") { model.openGuide() }.buttonStyle(.link)
                        }
                    }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(cream).clipShape(RoundedRectangle(cornerRadius: 14)).padding(.horizontal, 32)
                }
                if let connection = model.selected { detail(connection) }
                else { welcome }
            }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 900, minHeight: 610)
        .tint(moss)
        .sheet(isPresented: $showAdd) { ConnectionEditor(model: model, original: nil) }
        .sheet(item: $editing) { ConnectionEditor(model: model, original: $0) }
        .sheet(item: $model.drivePanel) { panel in
            switch panel.kind {
            case .photos: PhotoBrowserView(connection: panel.connection)
            case .settings: DriveSettingsView(model: model, connection: panel.connection)
            case .rename: RenameDriveView(model: model, connection: panel.connection)
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
        } message: { Text("This removes the connection from Mountain Turtle. Files in the S3 bucket stay where they are.") }
        .onReceive(NotificationCenter.default.publisher(for: .showTurtleWindow)) { _ in openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
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
                    }
                }.padding(.horizontal, 12)
            }
            Button { showAdd = true } label: {
                Label("Add connection", systemImage: "plus").frame(maxWidth: .infinity)
            }.controlSize(.large).padding(.horizontal, 20).padding(.vertical, 18)
            Divider().padding(.horizontal, 20)
            VStack(alignment: .leading, spacing: 14) {
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
            Text("Your buckets. On your Mac.").font(.system(size: 28, weight: .semibold)).foregroundStyle(ink)
            Text("Connect an S3 bucket and browse its files in Finder.\nDownload and cache files as you need them.")
                .font(.system(size: 14)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(5)
            Button("Add your first connection") { showAdd = true }.buttonStyle(.borderedProminent).controlSize(.large).padding(.top, 6)
            HStack(spacing: 24) {
                Label("AWS SSO", systemImage: "person.badge.key")
                Label("Finder drives", systemImage: "externaldrive")
                Label("No subscription", systemImage: "checkmark.seal")
            }.font(.system(size: 11)).foregroundStyle(moss).padding(.top, 16)
            Spacer()
            Text("Your files stay in your S3 bucket.").font(.system(size: 11)).foregroundStyle(.secondary).padding(.bottom, 30)
        }.frame(maxWidth: .infinity)
    }

    private func detail(_ connection: Connection) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .top, spacing: 18) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 20).fill(moss.opacity(0.08)).frame(width: 86, height: 86)
                        Image(systemName: "externaldrive.fill").font(.system(size: 43)).foregroundStyle(moss)
                        Text("S3").font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(cream).offset(y: -6)
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
                        Button("Browse photos…") { model.drivePanel = DrivePanel(connection: connection, kind: .photos) }
                        Button("Download & cache settings…") { model.drivePanel = DrivePanel(connection: connection, kind: .settings) }
                        Button("Rename drive…") { model.drivePanel = DrivePanel(connection: connection, kind: .rename) }
                        Divider()
                        Button("Refresh folder listings") { Task { await model.action(["refresh", connection.id]) } }.disabled(!connection.isConnected)
                        Button("Reconnect drive") { Task { await model.action(["reconnect", connection.id]) } }
                        Divider()
                        Button("Edit connection…") { editing = connection }.disabled(connection.isMounted || connection.desiredConnected || connection.isWorking)
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
                        Button("Sign in to AWS") { Task { await model.action(["login", connection.id]) } }
                    } else if connection.state == "needsLogin" {
                        Button { Task { await model.action(["login", connection.id]) } } label: { Label("Sign in to AWS", systemImage: "person.badge.key") }.buttonStyle(.borderedProminent)
                        Button("Try connecting again") { Task { await model.action(["connect", connection.id]) } }
                    } else {
                        Button { Task { await model.action(["connect", connection.id]) } } label: { Label(connection.state == "disconnecting" ? "Ejecting…" : connection.isWorking ? "Connecting…" : "Connect drive", systemImage: "bolt.horizontal") }.buttonStyle(.borderedProminent).disabled(connection.isWorking)
                        Button("Sign in to AWS") { Task { await model.action(["login", connection.id]) } }.disabled(connection.isWorking)
                    }
                    if !connection.isConnected && (connection.isMounted || connection.desiredConnected) {
                        Button(connection.isMounted ? "Eject" : "Cancel connection") { Task { await model.action(["disconnect", connection.id]) } }
                            .disabled(connection.state == "disconnecting")
                    }
                }.controlSize(.large).disabled(model.activeAction != nil)
                if model.activeAction == "login" {
                    HStack(spacing: 10) {
                        ProgressView().controlSize(.small)
                        Text("Approve the request on the AWS page within five minutes. Mountain Turtle will confirm when sign-in finishes.").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                if let message = model.loginMessage {
                    notice(message, symbol: "checkmark.circle", color: moss)
                }
                if let message = model.driveMessage {
                    notice(message, symbol: "checkmark.circle", color: moss)
                }
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
                Button { model.drivePanel = DrivePanel(connection: connection, kind: .settings) } label: {
                    Label("Download & cache settings", systemImage: "slider.horizontal.3")
                }.buttonStyle(.link)
                VStack(spacing: 0) {
                    infoRow("S3 bucket", connection.bucket, symbol: "shippingbox")
                    Divider().padding(.leading, 42)
                    infoRow("AWS profile", connection.profile, symbol: "person.crop.circle")
                    Divider().padding(.leading, 42)
                    infoRow("Region", connection.region, symbol: "globe.americas")
                    Divider().padding(.leading, 42)
                    infoRow("Access", connection.readOnly ? "Read only" : "Read & write", symbol: connection.readOnly ? "lock" : "pencil")
                    Divider().padding(.leading, 42)
                    infoRow("At login", connection.autoConnect ? "Reconnect this drive" : "Connect manually", symbol: "arrow.clockwise")
                }.padding(.horizontal, 16).background(cream.opacity(0.85)).clipShape(RoundedRectangle(cornerRadius: 16))
                HStack(alignment: .top, spacing: 11) {
                    Image(systemName: "leaf").font(.system(size: 19)).foregroundStyle(moss)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Download only as needed").font(.system(size: 13, weight: .semibold))
                        Text("Finder previews can read original photos. Use Browse photos to avoid those full downloads, or turn off icon previews in Finder’s View Options. AWS may occasionally ask you to sign in again.")
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

struct ConnectionEditor: View {
    @ObservedObject var model: AppModel
    var original: Connection?
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var bucket = ""
    @State private var profile = ""
    @State private var region = "us-east-1"
    @State private var readOnly = true
    @State private var autoConnect = false
    @State private var saving = false
    @State private var failure: String?

    var valid: Bool { !name.trimmingCharacters(in: .whitespaces).isEmpty && !bucket.trimmingCharacters(in: .whitespaces).isEmpty && !profile.isEmpty && !region.isEmpty }
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 13) {
                BrandIcon(size: 52)
                VStack(alignment: .leading, spacing: 5) {
                    Text(original == nil ? "Add an S3 drive" : "Edit connection").font(.system(size: 23, weight: .semibold))
                    Text("A saved AWS profile keeps your keys out of the app.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            Form {
                TextField("Drive name", text: $name, prompt: Text("e.g. My photos"))
                TextField("S3 bucket", text: $bucket, prompt: Text("your-bucket-name"))
                HStack {
                    TextField("AWS profile", text: $profile, prompt: Text("Choose a saved profile"))
                    Menu { ForEach(model.profiles, id: \.self) { p in Button(p) { profile = p } } } label: { Image(systemName: "chevron.down") }.menuStyle(.borderlessButton).frame(width: 20).disabled(model.profiles.isEmpty)
                }
                TextField("AWS region", text: $region)
            }.textFieldStyle(.roundedBorder).font(.system(size: 13))
            VStack(alignment: .leading, spacing: 13) {
                Toggle("Read only", isOn: $readOnly).toggleStyle(.checkbox)
                Text(readOnly ? "Browse and download. Changes to the bucket are disabled." : "Saving, moving, or deleting files in Finder changes the S3 bucket.")
                    .font(.system(size: 11)).foregroundStyle(.secondary).padding(.leading, 20)
                Toggle("Reconnect this drive at login", isOn: $autoConnect).toggleStyle(.checkbox)
                Text("Also enable “Restore drives at login” in the main window.").font(.system(size: 11)).foregroundStyle(.secondary).padding(.leading, 20)
            }.font(.system(size: 12)).padding(16).frame(maxWidth: .infinity, alignment: .leading).background(cream).clipShape(RoundedRectangle(cornerRadius: 12))
            if let failure {
                Label(failure, systemImage: "exclamationmark.circle").font(.system(size: 12)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(original == nil ? "Add connection" : "Save changes") {
                    saving = true
                    failure = nil
                    var args = original.map { ["edit", $0.id] } ?? ["add"]
                    args += ["--name", name.trimmingCharacters(in: .whitespacesAndNewlines), "--bucket", bucket.trimmingCharacters(in: .whitespacesAndNewlines), "--profile", profile, "--region", region]
                    if readOnly { args.append("--read-only") }
                    if autoConnect { args.append("--auto-connect") }
                    Task {
                        if await model.action(args) { dismiss() }
                        else { failure = model.error; model.error = nil }
                        saving = false
                    }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(!valid || saving)
            }
        }.padding(28).frame(width: 490).tint(moss)
        .onAppear {
            if let original { name = original.name; bucket = original.bucket; profile = original.profile; region = original.region; readOnly = original.readOnly; autoConnect = original.autoConnect }
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
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func application(_ application: NSApplication, open urls: [URL]) {
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
                CommandGroup(replacing: .newItem) { Button("Show connections") { NotificationCenter.default.post(name: .showTurtleWindow, object: nil) }.keyboardShortcut("n") }
                CommandGroup(replacing: .help) { Button("Mountain Turtle Help") { model.openGuide() } }
            }
    }
}
