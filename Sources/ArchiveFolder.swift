import AppKit
import SwiftUI

private struct ArchiveUpdate: Decodable {
    var phase: String
    var files: Int?
    var bytes: Int64?
    var speed: Double?
    var path: String?
    var message: String?
}

@MainActor final class ArchiveController: NSObject, ObservableObject, NSWindowDelegate {
    static let shared = ArchiveController()
    @Published var phase = "preparing"
    @Published var detail = "Connecting to the drive…"
    @Published var folder = ""
    @Published var files = 0
    @Published var bytes: Int64 = 0
    @Published var speed: Double = 0
    private var process: Process?
    private var window: NSWindow?
    private var destination: URL?
    private var choosing = false
    private var cancelling = false
    var running: Bool { process != nil }

    func chooseDestination(for path: String) {
        if running || choosing {
            window?.makeKeyAndOrderFront(nil)
            return
        }
        choosing = true
        let panel = NSSavePanel()
        panel.title = "Compress with Mountain Turtle"
        panel.message = "Save the ZIP on your Mac. Keep the source folder unchanged while downloading. Allow space for both the folder and ZIP."
        panel.nameFieldStringValue = URL(fileURLWithPath: path).lastPathComponent + ".zip"
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.canCreateDirectories = true
        panel.begin { [weak self] response in
            guard let self else { return }
            self.choosing = false
            if response == .OK, let url = panel.url { self.start(path: path, destination: url) }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    private func start(path: String, destination: URL) {
        phase = "preparing"; detail = "Connecting to the drive…"
        files = 0; bytes = 0; speed = 0; cancelling = false
        folder = URL(fileURLWithPath: path).lastPathComponent
        self.destination = destination
        let window = self.window ?? NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 340),
                                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Compress with Mountain Turtle"
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = NSHostingView(rootView: ArchiveProgressView(model: self))
        self.window = window
        window.center(); window.makeKeyAndOrderFront(nil)
        guard let python = ServiceClient.pythonPath else {
            phase = "failed"; detail = "Install Python 3 to compress this folder."; return
        }
        let child = Process(), output = Pipe()
        child.executableURL = URL(fileURLWithPath: python)
        child.arguments = ["-B", ServiceClient.resources.appendingPathComponent("service/archive_folder.py").path,
                           "--resource-dir", ServiceClient.resources.path, "--path", path,
                           "--destination", destination.path]
        child.standardOutput = output
        child.standardError = FileHandle.nullDevice
        child.standardInput = FileHandle.nullDevice
        do {
            try child.run()
            process = child
        } catch {
            phase = "failed"; detail = error.localizedDescription; return
        }
        DispatchQueue.global(qos: .utility).async {
            var buffer = Data()
            while true {
                let data = output.fileHandleForReading.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = Data(buffer[..<newline])
                    buffer.removeSubrange(...newline)
                    if let update = try? JSONDecoder().decode(ArchiveUpdate.self, from: line) {
                        DispatchQueue.main.async { self.receive(update) }
                    }
                }
            }
            child.waitUntilExit()
            DispatchQueue.main.async {
                self.process = nil
                if !["complete", "cancelled", "failed"].contains(self.phase) {
                    self.phase = self.cancelling ? "cancelled" : "failed"
                    self.detail = self.cancelling ? "Compression cancelled." : "Compression stopped before a ZIP was saved. Check the connection and try again."
                }
                self.objectWillChange.send()
            }
        }
    }

    private func receive(_ update: ArchiveUpdate) {
        phase = update.phase
        files = update.files ?? files; bytes = update.bytes ?? bytes; speed = update.speed ?? 0
        switch phase {
        case "downloading": detail = "Downloading directly from the drive…"
        case "compressing": detail = "Creating the ZIP on your Mac…"
        case "complete": detail = "Your ZIP is ready."
        case "cancelled": detail = "Compression cancelled. Temporary downloads were removed."
        case "failed": detail = update.message ?? "Compression failed."
        default: break
        }
        if cancelling && running && !["complete", "cancelled", "failed"].contains(phase) {
            detail = "Cancelling and removing temporary downloads…"
        }
    }

    func cancel() {
        guard let process, !cancelling else { return }
        cancelling = true
        detail = "Cancelling and removing temporary downloads…"
        process.terminate()
    }

    func reveal() {
        if let destination { NSWorkspace.shared.activateFileViewerSelecting([destination]) }
    }

    func close() { window?.performClose(nil) }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if running { cancel(); return false }
        return true
    }
}

private struct ArchiveProgressView: View {
    @ObservedObject var model: ArchiveController
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(model.folder, systemImage: "doc.zipper").font(.title2).lineLimit(2)
            Text(model.detail).fixedSize(horizontal: false, vertical: true)
            if model.running {
                ProgressView().progressViewStyle(.linear)
                Text("\(model.files.formatted()) files · \(ByteCountFormatter.string(fromByteCount: model.bytes, countStyle: .file))")
                    .monospacedDigit().foregroundStyle(.secondary)
                if model.speed > 0 {
                    Text("\(ByteCountFormatter.string(fromByteCount: Int64(model.speed), countStyle: .file))/s")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack {
                Spacer()
                if model.running { Button("Cancel") { model.cancel() } }
                else {
                    if model.phase == "complete" { Button("Show in Finder") { model.reveal() } }
                    Button("Close") { model.close() }.keyboardShortcut(.defaultAction)
                }
            }
        }.padding(24).frame(width: 480, alignment: .leading)
    }
}
