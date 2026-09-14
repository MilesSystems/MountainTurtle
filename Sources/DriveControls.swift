import SwiftUI
import AppKit

struct DrivePanel: Identifiable {
    enum Kind { case settings, rename, photos }
    let id = UUID()
    var connection: Connection
    var kind: Kind
}

struct CacheInformation: Decodable {
    var usedBytes: Int64
    var files: Int
    var partial: Bool

    var summary: String {
        let size = ByteCountFormatter.string(fromByteCount: usedBytes, countStyle: .file)
        return "\(partial ? "At least " : "")\(size) in \(files.formatted()) cached files"
    }
}

struct RenameDriveView: View {
    @ObservedObject var model: AppModel
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var failure: String?
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("Rename drive", systemImage: "externaldrive").font(.title2.weight(.semibold))
            Text("This is the name you see on your Mac. The S3 bucket keeps its name.")
                .font(.callout).foregroundStyle(.secondary)
            TextField("Drive name", text: $name).textFieldStyle(.roundedBorder)
            if connection.isMounted || connection.desiredConnected {
                Text("Mountain Turtle will safely eject and reconnect the drive to apply the name.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let failure { Label(failure, systemImage: "exclamationmark.circle").font(.callout).foregroundStyle(.red) }
            HStack {
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(saving ? "Renaming…" : "Rename drive") {
                    saving = true
                    Task {
                        if await model.updateDrive(connection, arguments: ["rename", connection.id, "--name=\(name.trimmingCharacters(in: .whitespacesAndNewlines))"]) { dismiss() }
                        else { failure = model.error; model.error = nil }
                        saving = false
                    }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || name == connection.name)
            }
        }.padding(28).frame(width: 460).tint(moss)
            .onAppear { name = connection.name }
            .interactiveDismissDisabled(saving)
    }
}

struct DriveSettingsView: View {
    @ObservedObject var model: AppModel
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var sizeMiB = 2048
    @State private var ageHours = 24
    @State private var info: CacheInformation?
    @State private var failure: String?
    @State private var saving = false
    @State private var loadingInfo = false
    @State private var confirmClear = false

    private var sizes: [Int] { Array(Set([256, 512, 1024, 2048, 5120, 10240, connection.cacheMaxSizeMiB ?? 2048])).sorted() }
    private var ages: [Int] { Array(Set([1, 6, 24, 72, 168, connection.cacheMaxAgeHours ?? 24])).sorted() }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("Download & cache settings", systemImage: "slider.horizontal.3").font(.title2.weight(.semibold))
            Text(connection.name).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 12) {
                Label("Small reads, no read-ahead", systemImage: "leaf").fontWeight(.medium)
                Text("Files are fetched when an app reads them. Finder’s thumbnails and Preview pane can read original photos, including photos outside the visible area.")
                    .font(.callout).foregroundStyle(.secondary)
                Text("For fewer downloads, use Browse photos here. In Finder, turn off Show icon preview (⌘J) and hide the Preview pane.")
                    .font(.callout).foregroundStyle(.secondary)
            }.padding(16).background(cream).clipShape(RoundedRectangle(cornerRadius: 12))
            Form {
                Picker("Original-file cache", selection: $sizeMiB) {
                    ForEach(sizes, id: \.self) { size in
                        Text(ByteCountFormatter.string(fromByteCount: Int64(size) * 1_048_576, countStyle: .binary)).tag(size)
                    }
                }
                Picker("Remove unused files after", selection: $ageHours) {
                    ForEach(ages, id: \.self) { age in Text(age == 1 ? "1 hour" : age < 24 ? "\(age) hours" : "\(age / 24) day\(age == 24 ? "" : "s")").tag(age) }
                }
            }
            Text("These are cache targets, not a download allowance. Files in use or waiting to upload are kept. The photo browser has a separate 256 MB thumbnail cache.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(info?.summary ?? "Checking local cache…").font(.callout)
                    Text("Original files on this Mac").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button { Task { await loadInfo() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("Refresh cache usage").disabled(loadingInfo || saving)
                Button("Clear cache…") { confirmClear = true }.disabled(saving || model.activeAction != nil)
            }
            if let failure { Label(failure, systemImage: "exclamationmark.circle").font(.callout).foregroundStyle(.red) }
            Divider()
            HStack {
                Button("Close", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(saving ? "Applying…" : "Save settings") { apply(clear: false) }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(saving || model.activeAction != nil)
            }
        }.padding(28).frame(width: 530).tint(moss)
            .task {
                sizeMiB = connection.cacheMaxSizeMiB ?? 2048
                ageHours = connection.cacheMaxAgeHours ?? 24
                await loadInfo()
            }
            .interactiveDismissDisabled(saving)
            .alert("Clear downloaded copies?", isPresented: $confirmClear) {
                Button("Cancel", role: .cancel) {}
                Button("Clear local cache", role: .destructive) { apply(clear: true) }
            } message: {
                Text("The drive will safely eject and reconnect. S3 originals stay in the bucket. Open files or pending uploads can prevent clearing. Finder may download previews again when you reopen a folder.")
            }
    }

    private func loadInfo() async {
        loadingInfo = true
        defer { loadingInfo = false }
        do { info = try JSONDecoder().decode(CacheInformation.self, from: await ServiceClient.run(["cache-info", connection.id])) }
        catch { failure = error.localizedDescription }
    }

    private func apply(clear: Bool) {
        saving = true; failure = nil
        let args = clear ? ["clear-cache", connection.id] : ["settings", connection.id, "--cache-max-size-mib", String(sizeMiB), "--cache-max-age-hours", String(ageHours)]
        Task {
            if await model.updateDrive(connection, arguments: args) {
                if clear { await loadInfo() } else { dismiss() }
            } else { failure = model.error; model.error = nil }
            saving = false
        }
    }
}
