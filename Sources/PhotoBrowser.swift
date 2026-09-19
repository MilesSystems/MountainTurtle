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
    var dateTaken: String?
}
struct OriginalPhoto: Decodable {
    var originalPath: String
    var dateTaken: String?
}

private struct PhotoDateResponse: Decodable { var dateTaken: String? }
private struct PhotoDateResult: Sendable {
    let identity: PhotoIdentity
    let date: PhotoTakenDate?
}

private enum PhotoDates {
    static func read(_ photo: PhotoItem, connectionID: String) async -> PhotoDateResult {
        do {
            try await ThumbnailSlots.shared.acquire()
            defer { Task { await ThumbnailSlots.shared.release() } }
            let result = try await PhotoClient.run(["date-taken", connectionID] + photo.arguments, as: PhotoDateResponse.self)
            try Task.checkCancellation()
            return PhotoDateResult(identity: photo.identity, date: PhotoTakenDate(result.dateTaken))
        } catch {
            return PhotoDateResult(identity: photo.identity, date: nil)
        }
    }
}

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
    @State private var originalRequestID = UUID()
    @State private var opening: String?
    @State private var visible: Set<String> = []
    @State private var previews = true
    @State private var originalRefreshes: [String: UUID] = [:]
    @State private var sort: PhotoPageSort = .name
    @State private var orderedPhotos: [PhotoItem] = []
    @State private var dates: [PhotoIdentity: PhotoTakenDate] = [:]
    @State private var dateTask: Task<Void, Never>?
    @State private var dateRequestID = UUID()
    @State private var readingDates = false
    @State private var dateFailures = 0

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
            HStack(spacing: 10) {
                Picker("Sort this page", selection: $sort) {
                    ForEach(PhotoPageSort.allCases) { choice in Text(choice.rawValue).tag(choice) }
                }.frame(width: 295)
                    .help("Sort only the photos on this page. Camera dates keep their recorded time; missing dates appear last.")
                if readingDates {
                    ProgressView().controlSize(.small)
                    Text("Reading dates on this page…")
                } else if dateFailures > 0 {
                    Text("\(dateFailures) dates could not be read.")
                    Button("Retry dates") { sortPage() }
                } else {
                    Text("Camera dates; missing dates are Unknown.")
                }
                Spacer(minLength: 0)
            }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 20).padding(.bottom, 12)
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
            .onChange(of: sort) { _, _ in sortPage() }
            .onDisappear { cancelPageRequests() }
    }

    private func gallery(_ page: PhotoPage) -> some View {
        let generation = requestID
        return GeometryReader { viewport in
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
                    ForEach(orderedPhotos) { photo in
                        PhotoTile(connectionID: connection.id, photo: photo, visible: previews && visible.contains(photo.key),
                                  originalRefresh: originalRefreshes[photo.key], opening: opening == photo.key,
                                  dateTaken: dates[photo.identity] ?? PhotoTakenDate(nil),
                                  receivedDate: { value in recordDate(value, for: photo, generation: generation) },
                                  openOriginal: { open(photo, reveal: false) }, downloadOriginal: { open(photo, reveal: true) })
                            .background(GeometryReader { geometry in
                                Color.clear.preference(key: PhotoFrames.self, value: [photo.key: geometry.frame(in: .named("photoViewport"))])
                            })
                            .id(photo.identity)
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
                Button("Cancel download") {
                    originalTask?.cancel(); originalRequestID = UUID()
                    opening = nil; originalMessage = "Download cancelled."
                }
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
    private func cancelPageRequests() {
        dateTask?.cancel(); dateRequestID = UUID(); readingDates = false
        originalTask?.cancel(); originalRequestID = UUID(); opening = nil
    }
    private func loadPage() async {
        let generation = requestID
        cancelPageRequests()
        loading = true; failure = nil; visible = []; originalRefreshes = [:]
        dates = [:]; orderedPhotos = []; dateFailures = 0; originalMessage = nil
        do {
            var args = ["list", connection.id, "--prefix=\(prefix)", "--limit", "100"]
            if let cursor { args.append("--cursor=\(cursor)") }
            let result = try await PhotoClient.run(args, as: PhotoPage.self)
            try Task.checkCancellation()
            guard generation == requestID else { return }
            page = result; loading = false
            orderedPhotos = PhotoDateOrdering.sorted(result.photos, by: .name, dates: dates)
            sortPage()
        } catch {
            if !Task.isCancelled && generation == requestID {
                failure = error.localizedDescription; page = nil; loading = false
            }
        }
    }

    private func recordDate(_ value: String?, for photo: PhotoItem, generation: UUID) {
        guard generation == requestID, page?.photos.contains(where: { $0.identity == photo.identity }) == true else { return }
        let date = PhotoTakenDate(value)
        // A cached preview without a date must not overwrite a date obtained
        // later from the explicitly downloaded original.
        guard date.value != nil || dates[photo.identity]?.value == nil else { return }
        let changed = dates[photo.identity]?.value != date.value
        dates[photo.identity] = date
        if changed && !readingDates && sort != .name, let page {
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: sort, dates: dates)
        }
    }

    private func sortPage() {
        dateTask?.cancel()
        let generation = UUID()
        dateRequestID = generation
        readingDates = false; dateFailures = 0
        guard let page, !loading else { return }
        guard sort != .name else {
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: .name, dates: dates)
            return
        }
        let pageGeneration = requestID
        let choice = sort
        let connectionID = connection.id
        // Only the listed page participates. Known camera dates need no request;
        // an Unknown preview can still gain a date from a local original.
        let pending = page.photos.filter { dates[$0.identity]?.value == nil }
        guard !pending.isEmpty else {
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: choice, dates: dates)
            return
        }
        readingDates = true
        dateTask = Task {
            let results = await withTaskGroup(of: PhotoDateResult.self) { group in
                var remaining = pending.makeIterator()
                for _ in 0..<2 {
                    if let photo = remaining.next() {
                        group.addTask { await PhotoDates.read(photo, connectionID: connectionID) }
                    }
                }
                var results: [PhotoDateResult] = []
                for await result in group {
                    if Task.isCancelled { group.cancelAll(); break }
                    results.append(result)
                    if let photo = remaining.next() {
                        group.addTask { await PhotoDates.read(photo, connectionID: connectionID) }
                    }
                }
                return results
            }
            guard !Task.isCancelled, pageGeneration == requestID, generation == dateRequestID else { return }
            for result in results {
                if let date = result.date {
                    if date.value != nil || dates[result.identity]?.value == nil { dates[result.identity] = date }
                } else {
                    dateFailures += 1
                }
            }
            // Publish one ordered page after the batch, keeping tile order
            // stable while previews and date requests complete out of order.
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: choice, dates: dates)
            readingDates = false
        }
    }

    private func open(_ photo: PhotoItem, reveal: Bool) {
        originalTask?.cancel()
        let generation = requestID
        let originalGeneration = UUID()
        originalRequestID = originalGeneration
        opening = photo.key; originalMessage = "Downloading \(photo.name)…"
        originalTask = Task {
            do {
                let result = try await PhotoClient.run(["open-original", connection.id] + photo.arguments, as: OriginalPhoto.self)
                try Task.checkCancellation()
                guard generation == requestID, originalGeneration == originalRequestID else { return }
                let url = URL(fileURLWithPath: result.originalPath)
                recordDate(result.dateTaken, for: photo, generation: generation)
                originalRefreshes[photo.key] = UUID()
                originalMessage = "Saved \(photo.name) in Downloads / Mountain Turtle."
                DispatchQueue.global(qos: .userInitiated).async {
                    if reveal { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                    else { NSWorkspace.shared.open(url) }
                }
            } catch {
                if !Task.isCancelled && generation == requestID && originalGeneration == originalRequestID {
                    originalMessage = error.localizedDescription
                }
            }
            if !Task.isCancelled && generation == requestID && originalGeneration == originalRequestID { opening = nil }
        }
    }
}

private struct PhotoTile: View {
    let connectionID: String
    let photo: PhotoItem
    let visible: Bool
    let originalRefresh: UUID?
    let opening: Bool
    let dateTaken: PhotoTakenDate
    let receivedDate: (String?) -> Void
    let openOriginal: () -> Void
    let downloadOriginal: () -> Void
    @State private var image: NSImage?
    @State private var loading = false
    @State private var message = "Online only"
    @State private var needsOriginal = false
    @State private var createTask: Task<Void, Never>?
    @State private var loadID = UUID()
    @State private var refreshedOriginal: UUID?

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
            Text(dateTaken.label).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                .frame(minHeight: 26)
                .help("\(dateTaken.label)\nThe camera's recorded date and time. Unknown means no camera date is available.")
            Text(image == nil ? (needsOriginal ? "Preview not available" : message) : "Small preview cached")
                .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
        }
        .accessibilityRepresentation {
            Button("\(photo.name), \(dateTaken.label), \(image == nil ? message : "small preview cached")", action: openOriginal)
                .accessibilityAction(named: Text("Download original"), downloadOriginal)
        }
        .help("\(photo.name) · \(ByteCountFormatter.string(fromByteCount: photo.size, countStyle: .file))\n\(dateTaken.label)\n\(message)\nDouble-click to download and open the original.")
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
            if let originalRefresh, originalRefresh != refreshedOriginal {
                // An explicit original download refreshes this tile from local
                // files only; it must not start another S3 request.
                await load(allowOriginal: false, cacheOnly: true)
                if !Task.isCancelled { refreshedOriginal = originalRefresh }
            } else if visible && image == nil {
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
            receivedDate(result.dateTaken)
            if let path = result.thumbnailPath { image = NSImage(contentsOfFile: path) }
            needsOriginal = result.needsOriginal ?? false
            message = result.message ?? (image == nil ? "Online only" : "Small preview cached")
        } catch { if !Task.isCancelled && loadID == generation { message = error.localizedDescription } }
        if loadID == generation { loading = false }
    }
}
