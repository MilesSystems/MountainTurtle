import SwiftUI
import AppKit

private struct OpenHandle: Decodable, Identifiable {
    let id: String
    let pid: Int32
    let command: String
    let owner: String
    let relativePath: String
    let kind: String
    let access: String
    let descriptor: String
    let handleCount: Int

    var app: NSRunningApplication? { NSRunningApplication(processIdentifier: pid) }
    var displayOwner: String {
        owner.hasPrefix("Mountain Turtle ") ? owner : (app?.localizedName ?? owner)
    }
}

private struct OpenHandleSnapshot: Decodable {
    let entries: [OpenHandle]
    let partial: Bool
    let checkedAt: Double
    let message: String
}

struct OpenFilesView: View {
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var snapshot: OpenHandleSnapshot?
    @State private var loading = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Open files & apps", systemImage: "doc.text.magnifyingglass").font(.title2.weight(.semibold))
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text(connection.name).foregroundStyle(.secondary)
            Text("See which apps have files or folders open on this drive. Save your work in those apps before closing them.")
                .font(.callout).foregroundStyle(.secondary)
            if loading { ProgressView("Inspecting open handles…").controlSize(.small) }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if snapshot?.entries.isEmpty == true {
                        Label(snapshot?.partial == true ? "No handles found in this limited snapshot" : "No open handles found", systemImage: "tray")
                            .font(.headline).padding(.vertical, 16)
                        Text("This does not guarantee the drive can be ejected. Short-lived handles and other users’ processes may not be visible.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    ForEach(snapshot?.entries ?? []) { handle in
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: handle.kind == "Folder" ? "folder" : "doc").foregroundStyle(moss).frame(width: 22)
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(handle.displayOwner).font(.headline)
                                    Text("PID \(handle.pid)").font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    if handle.app != nil {
                                        Button("Show app") { handle.app?.activate(options: []) }
                                    }
                                }
                                Text(handle.relativePath).font(.callout.monospaced()).textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                                Text("\(handle.kind) · \(handle.access) · \(handle.handleCount) handle(s)"
                                     + (handle.descriptor == "cwd" ? " · Working directory" : ""))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Divider()
                    }
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            }.background(cream).clipShape(RoundedRectangle(cornerRadius: 10))
            if let snapshot {
                Text(snapshot.message).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Text("Snapshot: \(Date(timeIntervalSince1970: snapshot.checkedAt).formatted(date: .omitted, time: .standard))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let errorMessage { Text(errorMessage).font(.callout).foregroundStyle(.orange) }
            HStack {
                Text("Inspection only. No processes are stopped or files changed.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Refresh") { Task { await refresh() } }.disabled(loading)
            }
        }.padding(24).frame(width: 760, height: 650)
        .task { await refresh() }
    }

    @MainActor private func refresh() async {
        loading = true
        defer { loading = false }
        do {
            let data = try await ServiceClient.run(["open-files", connection.id])
            snapshot = try JSONDecoder().decode(OpenHandleSnapshot.self, from: data)
            errorMessage = nil
        } catch { errorMessage = "Could not inspect open files. Try refreshing. No processes were stopped." }
    }
}
