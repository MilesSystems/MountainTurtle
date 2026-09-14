import SwiftUI
import AppKit

private final class PhotoProcess: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    func start(_ process: Process) throws {
        lock.lock(); defer { lock.unlock() }
        if cancelled { throw CancellationError() }
        try process.run()
        self.process = process
    }
    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

private actor ThumbnailSlots {
    static let shared = ThumbnailSlots()
    private var active = 0
    func acquire() async throws {
        while active >= 2 { try await Task.sleep(nanoseconds: 80_000_000) }
        try Task.checkCancellation()
        active += 1
    }
    func release() { active -= 1 }
}

enum PhotoClient {
    static func run<T: Decodable>(_ args: [String], as type: T.Type) async throws -> T {
        let handle = PhotoProcess()
        let data: Data = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        guard let python = ServiceClient.pythonPath else { throw TurtleError(message: "Python is unavailable.") }
                        let process = Process(), output = Pipe()
                        process.executableURL = URL(fileURLWithPath: python)
                        process.arguments = [ServiceClient.resources.appendingPathComponent("service/photo_browser.py").path,
                                             "--resource-dir", ServiceClient.resources.path] + args
                        process.standardOutput = output
                        process.standardError = FileHandle.nullDevice
                        try handle.start(process)
                        let data = output.fileHandleForReading.readDataToEndOfFile()
                        process.waitUntilExit()
                        if process.terminationStatus != 0 {
                            let response = try? JSONDecoder().decode(ActionResponse.self, from: data)
                            throw TurtleError(message: response?.error ?? "The photo request could not finish. Try again, or renew your AWS sign-in.")
                        }
                        continuation.resume(returning: data)
                    } catch { continuation.resume(throwing: error) }
                }
            }
        } onCancel: { handle.cancel() }
        try Task.checkCancellation()
        return try JSONDecoder().decode(type, from: data)
    }
}

struct PhotoFolder: Decodable, Identifiable {
    var key: String
    var name: String
    var id: String { key }
}
struct PhotoItem: Decodable, Identifiable {
    var key: String
    var name: String
    var size: Int64
    var etag: String
    var id: String { key }
    var arguments: [String] { ["--key=\(key)", "--etag=\(etag)", "--size=\(size)"] }
}
struct PhotoPage: Decodable {
    var prefix: String
    var folders: [PhotoFolder]
    var photos: [PhotoItem]
    var nextCursor: String?
    var hiddenFileCount: Int?
}
struct PhotoPreview: Decodable {
    var thumbnailPath: String?
    var source: String?
    var downloadedBytes: Int?
    var needsOriginal: Bool?
    var message: String?
}
struct OriginalPhoto: Decodable { var originalPath: String }

private struct PhotoFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) { value.merge(nextValue(), uniquingKeysWith: { _, last in last }) }
}

private struct PhotoPreviewRequest: Equatable {
    let visible: Bool
    let originalRefresh: UUID?
}

struct PhotoBrowserView: View {
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var page: PhotoPage?
    @State private var prefix = ""
    @State private var cursor: String?
    @State private var history: [String?] = []
    @State private var requestID = UUID()
    @State private var loading = false
    @State private var failure: String?
    @State private var originalMessage: String?
    @State private var originalTask: Task<Void, Never>?
    @State private var opening: String?
    @State private var visible: Set<String> = []
    @State private var previews = true
    @State private var originalRefreshes: [String: UUID] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "photo.on.rectangle.angled").font(.title2).foregroundStyle(moss)
                VStack(alignment: .leading, spacing: 3) {
                    Text(connection.name).font(.title3.weight(.semibold))
                    Text("Photo browser").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("Small previews", isOn: $previews).toggleStyle(.checkbox).font(.callout)
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(20)
            HStack(spacing: 8) {
                Button { navigate(to: "") } label: { Image(systemName: "house") }.help("Bucket root")
                Button { navigate(to: parentPrefix) } label: { Image(systemName: "chevron.up") }.disabled(prefix.isEmpty).help("Parent folder")
                Text(prefix.isEmpty ? "All folders" : prefix).font(.callout).lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                Spacer()
                Button { requestID = UUID() } label: { Image(systemName: "arrow.clockwise") }.help("Reload this page")
            }.padding(.horizontal, 20).padding(.bottom, 14)
            Text("Only visible tiles request previews. Missing previews stay online until you choose to create one or open the original.")
                .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 20).padding(.bottom, 12)
            Divider()
            if let failure {
                VStack(alignment: .leading, spacing: 10) {
                    Label(failure, systemImage: "exclamationmark.circle").foregroundStyle(.orange)
                    Button("Try again") { requestID = UUID() }
                }.padding(20)
            }
            if loading {
                ProgressView("Loading this page…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let page {
                gallery(page)
            } else {
                Spacer()
            }
            Divider()
            footer.padding(16)
        }.frame(width: 860, height: 660).tint(moss)
            .task(id: requestID) { await loadPage() }
            .onDisappear { originalTask?.cancel() }
    }

    private func gallery(_ page: PhotoPage) -> some View {
        GeometryReader { viewport in
            ScrollView {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 148), spacing: 16)], spacing: 18) {
                    ForEach(page.folders) { folder in
                        Button { navigate(to: folder.key) } label: {
                            VStack(spacing: 10) {
                                Image(systemName: "folder.fill").font(.system(size: 60)).foregroundStyle(moss.opacity(0.65)).frame(height: 106)
                                Text(folder.name).font(.callout).lineLimit(2).frame(height: 32)
                            }.frame(maxWidth: .infinity).contentShape(Rectangle())
                        }.buttonStyle(.plain).help("Open \(folder.name)")
                    }
                    ForEach(page.photos) { photo in
                        PhotoTile(connectionID: connection.id, photo: photo, visible: previews && visible.contains(photo.key),
                                  originalRefresh: originalRefreshes[photo.key], opening: opening == photo.key,
                                  openOriginal: { open(photo, reveal: false) }, downloadOriginal: { open(photo, reveal: true) })
                            .background(GeometryReader { geometry in
                                Color.clear.preference(key: PhotoFrames.self, value: [photo.key: geometry.frame(in: .named("photoViewport"))])
                            })
                    }
                }.padding(20)
                if page.photos.isEmpty && page.folders.isEmpty {
                    ContentUnavailableView("No photos on this page", systemImage: "photo", description: Text("Try another folder or the next page."))
                        .padding(30)
                }
            }.coordinateSpace(name: "photoViewport")
                .onPreferenceChange(PhotoFrames.self) { frames in
                    let bounds = CGRect(origin: .zero, size: viewport.size)
                    visible = Set(frames.compactMap { $0.value.intersects(bounds) ? $0.key : nil })
                }
        }.id(requestID)
    }

    private var footer: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(page.map { "\($0.photos.count) photos · \($0.folders.count) folders on this page" } ?? "One page at a time")
                if let count = page?.hiddenFileCount, count > 0 { Text("\(count) other files available in Finder").foregroundStyle(.secondary) }
                if let originalMessage { Text(originalMessage).foregroundStyle(moss).lineLimit(2) }
            }.font(.caption)
            Spacer()
            if opening != nil {
                ProgressView().controlSize(.small)
                Button("Cancel download") { originalTask?.cancel(); opening = nil; originalMessage = "Download cancelled." }
            }
            Button("Previous") {
                cursor = history.removeLast(); requestID = UUID()
            }.disabled(history.isEmpty || loading)
            Button("Next page") {
                guard let next = page?.nextCursor else { return }
                history.append(cursor); cursor = next; requestID = UUID()
            }.disabled(page?.nextCursor == nil || loading)
        }
    }

    private var parentPrefix: String {
        let parts = prefix.split(separator: "/").dropLast()
        return parts.isEmpty ? "" : parts.joined(separator: "/") + "/"
    }
    private func navigate(to next: String) {
        prefix = next; cursor = nil; history = []; visible = []; requestID = UUID()
    }
    private func loadPage() async {
        loading = true; failure = nil; visible = []; originalRefreshes = [:]
        do {
            var args = ["list", connection.id, "--prefix=\(prefix)", "--limit", "100"]
            if let cursor { args.append("--cursor=\(cursor)") }
            let result = try await PhotoClient.run(args, as: PhotoPage.self)
            try Task.checkCancellation()
            page = result; loading = false
        } catch {
            if !Task.isCancelled { failure = error.localizedDescription; page = nil; loading = false }
        }
    }
    private func open(_ photo: PhotoItem, reveal: Bool) {
        originalTask?.cancel()
        opening = photo.key; originalMessage = "Downloading \(photo.name)…"
        originalTask = Task {
            do {
                let result = try await PhotoClient.run(["open-original", connection.id] + photo.arguments, as: OriginalPhoto.self)
                try Task.checkCancellation()
                let url = URL(fileURLWithPath: result.originalPath)
                originalRefreshes[photo.key] = UUID()
                originalMessage = "Saved \(photo.name) in Downloads / Mountain Turtle."
                DispatchQueue.global(qos: .userInitiated).async {
                    if reveal { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                    else { NSWorkspace.shared.open(url) }
                }
            } catch { if !Task.isCancelled { originalMessage = error.localizedDescription } }
            if !Task.isCancelled { opening = nil }
        }
    }
}

private struct PhotoTile: View {
    let connectionID: String
    let photo: PhotoItem
    let visible: Bool
    let originalRefresh: UUID?
    let opening: Bool
    let openOriginal: () -> Void
    let downloadOriginal: () -> Void
    @State private var image: NSImage?
    @State private var loading = false
    @State private var message = "Online only"
    @State private var needsOriginal = false
    @State private var createTask: Task<Void, Never>?
    @State private var loadID = UUID()

    var body: some View {
        VStack(spacing: 8) {
            ZStack(alignment: .bottomTrailing) {
                RoundedRectangle(cornerRadius: 10).fill(cream).frame(height: 110)
                if let image {
                    Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: 106).frame(height: 110)
                } else {
                    Image(systemName: "photo").font(.system(size: 34)).foregroundStyle(moss.opacity(0.4)).frame(maxWidth: .infinity).frame(height: 110)
                }
                Image(systemName: image == nil ? "cloud.fill" : "checkmark.circle.fill")
                    .symbolRenderingMode(.palette).foregroundStyle(image == nil ? Color.gray : moss, Color.white).padding(5)
                if loading || opening { ProgressView().controlSize(.small).frame(maxWidth: .infinity).frame(height: 110) }
            }.contentShape(Rectangle()).onTapGesture(count: 2, perform: openOriginal)
            Text(photo.name).font(.callout).lineLimit(1).truncationMode(.middle)
            Text(image == nil ? (needsOriginal ? "Preview not available" : message) : "Small preview cached")
                .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
        }
        .accessibilityRepresentation {
            Button("\(photo.name), \(image == nil ? message : "small preview cached")", action: openOriginal)
                .accessibilityAction(named: Text("Download original"), downloadOriginal)
        }
        .help("\(photo.name) · \(ByteCountFormatter.string(fromByteCount: photo.size, countStyle: .file))\n\(message)\nDouble-click to download and open the original.")
        .contextMenu {
            Button("Open original", action: openOriginal)
            Button("Download original", action: downloadOriginal)
            if image == nil {
                Divider()
                Button("Create preview (downloads original)") {
                    createTask?.cancel()
                    createTask = Task { await load(allowOriginal: true) }
                }.disabled(photo.size > 32 * 1024 * 1024)
            }
        }
        .task(id: PhotoPreviewRequest(visible: visible, originalRefresh: originalRefresh)) {
            if visible && image == nil {
                // An explicit original download refreshes this tile from local
                // files only; it must not start another S3 request.
                await load(allowOriginal: false, cacheOnly: originalRefresh != nil)
            }
        }
        .onDisappear { createTask?.cancel() }
    }

    private func load(allowOriginal: Bool, cacheOnly: Bool = false) async {
        let generation = UUID()
        loadID = generation
        loading = true
        do {
            try await ThumbnailSlots.shared.acquire()
            defer { Task { await ThumbnailSlots.shared.release() } }
            var args = ["thumbnail", connectionID] + photo.arguments + ["--pixels", "256"]
            if allowOriginal { args.append("--allow-original") }
            if cacheOnly { args.append("--cache-only") }
            let result = try await PhotoClient.run(args, as: PhotoPreview.self)
            try Task.checkCancellation()
            guard loadID == generation else { return }
            if let path = result.thumbnailPath { image = NSImage(contentsOfFile: path) }
            needsOriginal = result.needsOriginal ?? false
            message = result.message ?? (image == nil ? "Online only" : "Small preview cached")
        } catch { if !Task.isCancelled && loadID == generation { message = error.localizedDescription } }
        if loadID == generation { loading = false }
    }
}
