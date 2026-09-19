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
    static func run<T: Decodable>(_ args: [String], as type: T.Type, script: String = "photo_browser.py") async throws -> T {
        let handle = PhotoProcess()
        let data: Data = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        guard let python = ServiceClient.pythonPath else { throw TurtleError(message: "Python is unavailable.") }
                        let process = Process(), output = Pipe()
                        process.executableURL = URL(fileURLWithPath: python)
                        process.arguments = ["-B", ServiceClient.resources.appendingPathComponent("service/\(script)").path,
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
    var dateState: String?
}
struct OriginalPhoto: Decodable {
    var originalPath: String
    var dateTaken: String?
    var dateState: String?
}

private struct PhotoDateResponse: Decodable { var dateTaken: String?; var dateState: String? }
private struct OfflineOpenResponse: Decodable { let path: String }
private struct PhotoDateResult: Sendable {
    let identity: PhotoIdentity
    let date: PhotoTakenDate
}

private enum PhotoDates {
    static func read(_ photo: PhotoItem, connectionID: String) async -> PhotoDateResult {
        do {
            try await ThumbnailSlots.shared.acquire()
            defer { Task { await ThumbnailSlots.shared.release() } }
            let result = try await PhotoClient.run(["date-taken", connectionID] + photo.arguments, as: PhotoDateResponse.self)
            try Task.checkCancellation()
            return PhotoDateResult(identity: photo.identity, date: PhotoTakenDate(result.dateTaken, state: result.dateState))
        } catch {
            return PhotoDateResult(identity: photo.identity, date: PhotoTakenDate(nil, state: "error"))
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
    @State private var selected: Set<PhotoIdentity> = []
    @State private var offlineQueue: OfflinePhotoQueue?
    @State private var offlineFailure: String?
    @State private var offlineBusy = false
    @State private var showQueue = false
    @State private var refreshedOffline: Set<String> = []

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
            Text("Previews are temporary. Keep offline saves originals on this Mac, outside the cache, until you delete them.")
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
                    Button("Retry dates") { sortPage(forceRead: true) }
                } else {
                    Text("Camera dates; unchecked and missing dates appear last.")
                }
                Spacer(minLength: 0)
            }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 20).padding(.bottom, 12)
            HStack(spacing: 10) {
                Button("Select page") { selected = Set(page?.photos.map(\.identity) ?? []) }
                    .disabled(page?.photos.isEmpty != false || loading)
                    .keyboardShortcut("a", modifiers: .command)
                Button("Clear") { selected = [] }.disabled(selected.isEmpty)
                Text("\(selected.count) selected").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button { keepOffline(page?.photos.filter { selected.contains($0.identity) } ?? []) } label: {
                    Label("Keep offline", systemImage: "arrow.down.circle")
                }.disabled(selected.isEmpty || offlineBusy)
            }.padding(.horizontal, 20).padding(.bottom, 12)
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
            offlinePanel
            Divider()
            footer.padding(16)
        }.frame(width: 920, height: 760).tint(moss)
            .task(id: requestID) { await loadPage() }
            .task { await observeOfflineQueue() }
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
                                  selected: selected.contains(photo.identity), offlineItem: offlineQueue?.item(for: photo),
                                  toggleSelection: { if !selected.insert(photo.identity).inserted { selected.remove(photo.identity) } },
                                  receivedDate: { value in recordDate(value, for: photo, generation: generation) },
                                  checkDate: { checkDate(photo) },
                                  openOriginal: { openPhoto(photo) }, keepOffline: { keepOffline([photo]) },
                                  revealOffline: { if let item = offlineQueue?.item(for: photo) { openOffline(item, reveal: true) } })
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

    private var offlinePanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Button { showQueue.toggle() } label: {
                    Label(offlineQueue?.summary ?? "Offline downloads", systemImage: showQueue ? "chevron.down" : "chevron.right")
                }.buttonStyle(.plain).font(.callout.weight(.medium))
                Spacer()
                if offlineBusy { ProgressView().controlSize(.small) }
                if let queue = offlineQueue, queue.pendingCount > 0 {
                    if queue.paused {
                        Button("Resume") { offlineAction("resume") }.disabled(offlineBusy)
                    } else if queue.workerRunning {
                        Button("Pause") { offlineAction("pause") }.disabled(offlineBusy)
                    } else {
                        Button("Resume downloads") { offlineAction("resume") }.disabled(offlineBusy)
                    }
                }
            }
            if let offlineFailure {
                HStack {
                    Label(offlineFailure, systemImage: "exclamationmark.circle").foregroundStyle(.orange).lineLimit(2)
                    Button("Retry status") { Task { await refreshOfflineQueue() } }
                }.font(.caption)
            }
            if showQueue {
                Text("Downloads continue when this window closes. Offline copies stay outside the temporary cache.")
                    .font(.caption).foregroundStyle(.secondary)
                if let queue = offlineQueue, !queue.items.isEmpty {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(queue.items) { item in offlineRow(item) }
                        }.padding(.vertical, 4)
                    }.frame(height: min(170, CGFloat(queue.items.count) * 70))
                } else {
                    Text("Select photos, then choose Keep offline.").font(.caption).foregroundStyle(.secondary)
                }
            }
        }.padding(.horizontal, 20).padding(.vertical, 12)
    }

    private func offlineRow(_ item: OfflinePhotoItem) -> some View {
        HStack(spacing: 12) {
            Image(systemName: item.isVerified ? "checkmark.shield.fill" : (item.hasOfflineCopy ? "internaldrive" : "arrow.down.circle"))
                .foregroundStyle(item.isVerified ? moss : .secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.name).font(.callout).lineLimit(1).truncationMode(.middle)
                if item.isPending {
                    ProgressView(value: item.progress).frame(maxWidth: 420)
                    Text("\(offlineQueue?.paused == true ? (offlineQueue?.workerRunning == true ? "Pausing…" : "Paused") : item.stateLabel) · \(item.progressLabel)")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text(item.error ?? item.stateLabel).font(.caption)
                        .foregroundStyle(item.state == "error" ? .orange : .secondary).lineLimit(2)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            if item.state == "error" {
                Button("Retry") { offlineAction("retry", item: item) }.disabled(offlineBusy)
            }
            if item.hasOfflineCopy {
                Button("Open") { openOffline(item, reveal: false) }.disabled(offlineBusy)
                Button { openOffline(item, reveal: true) } label: { Image(systemName: "folder") }
                    .help("Show offline copy in Finder").accessibilityLabel("Show \(item.name) in Finder")
                    .disabled(offlineBusy)
            }
            if item.sha256 != nil && (item.hasOfflineCopy || item.state == "error") {
                Button { offlineAction("verify", item: item) } label: {
                    if item.state == "error" { Text("Check copy") }
                    else { Image(systemName: "checkmark.shield") }
                }
                    .help("Check that the local copy still matches its saved checksum. This does not add a missing cloud checksum.")
                    .accessibilityLabel("Check local copy of \(item.name)").disabled(offlineBusy)
            }
        }.help(item.state == "downloaded" ? "This original is retained offline. The cloud supplied no supported checksum, so end-to-end verification is unavailable." : (item.error ?? item.stateLabel))
    }

    private func observeOfflineQueue() async {
        while !Task.isCancelled {
            if !offlineBusy { await refreshOfflineQueue() }
            do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
        }
    }

    private func refreshOfflineQueue() async {
        do {
            let queue = try await PhotoClient.run(["status", connection.id], as: OfflinePhotoQueue.self, script: "offline_photos.py")
            try Task.checkCancellation()
            guard !offlineBusy else { return }
            applyOfflineQueue(queue)
            offlineFailure = nil
        } catch {
            if !Task.isCancelled && !offlineBusy { offlineFailure = error.localizedDescription }
        }
    }

    private func applyOfflineQueue(_ queue: OfflinePhotoQueue) {
        if offlineQueue == nil && !queue.items.isEmpty { showQueue = true }
        offlineQueue = queue
        for item in queue.items where item.hasOfflineCopy && !refreshedOffline.contains(item.id) {
            guard let photo = page?.photos.first(where: { $0.identity == item.identity }) else { continue }
            refreshedOffline.insert(item.id)
            originalRefreshes[photo.key] = UUID()
            checkDate(photo)
        }
    }

    private func keepOffline(_ photos: [PhotoItem]) {
        guard !photos.isEmpty, !offlineBusy else { return }
        do {
            let items: [[String: Any]] = photos.map { ["key": $0.key, "etag": $0.etag, "size": $0.size] }
            let data = try JSONSerialization.data(withJSONObject: items)
            offlineAction("enqueue", extra: ["--items-json", String(decoding: data, as: UTF8.self)])
            showQueue = true
        } catch { offlineFailure = error.localizedDescription }
    }

    private func offlineAction(_ action: String, item: OfflinePhotoItem? = nil, extra: [String] = []) {
        guard !offlineBusy else { return }
        offlineBusy = true; offlineFailure = nil
        var args = [action, connection.id] + extra
        if let item { args += ["--item-id", item.id] }
        Task {
            defer { offlineBusy = false }
            do {
                let result = try await PhotoClient.run(args, as: OfflinePhotoQueue.self, script: "offline_photos.py")
                applyOfflineQueue(result)
                if action == "enqueue" { selected = [] }
            } catch { offlineFailure = error.localizedDescription }
        }
    }

    private func openPhoto(_ photo: PhotoItem) {
        if let item = offlineQueue?.item(for: photo), item.hasOfflineCopy { openOffline(item, reveal: false) }
        else { open(photo, reveal: false) }
    }

    private func openOffline(_ item: OfflinePhotoItem, reveal: Bool) {
        guard !offlineBusy else { return }
        offlineBusy = true; offlineFailure = nil
        Task {
            defer { offlineBusy = false }
            do {
                let result = try await PhotoClient.run(["open", connection.id, "--item-id", item.id], as: OfflineOpenResponse.self, script: "offline_photos.py")
                let url = URL(fileURLWithPath: result.path)
                if reveal { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                else { NSWorkspace.shared.open(url) }
            } catch { offlineFailure = error.localizedDescription }
        }
    }

    private func checkDate(_ photo: PhotoItem) {
        let generation = requestID
        Task {
            let result = await PhotoDates.read(photo, connectionID: connection.id)
            guard generation == requestID else { return }
            recordDate(result.date, for: photo, generation: generation)
            dateFailures = dates.values.filter { $0.state == .error }.count
        }
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
        loading = true; failure = nil; visible = []; originalRefreshes = [:]; selected = []
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

    private func recordDate(_ value: PhotoTakenDate, for photo: PhotoItem, generation: UUID) {
        guard generation == requestID, page?.photos.contains(where: { $0.identity == photo.identity }) == true else { return }
        let date = value.preservingMoreComplete(dates[photo.identity])
        // A cached preview without a date must not overwrite a date obtained
        // later from the explicitly downloaded original.
        guard date.value != nil || dates[photo.identity]?.value == nil else { return }
        let changed = dates[photo.identity]?.value != date.value
        dates[photo.identity] = date
        dateFailures = dates.values.filter { $0.state == .error }.count
        if changed && !readingDates && sort != .name, let page {
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: sort, dates: dates)
        }
    }

    private func sortPage(forceRead: Bool = false) {
        dateTask?.cancel()
        let generation = UUID()
        dateRequestID = generation
        readingDates = false; dateFailures = 0
        guard let page, !loading else { return }
        guard sort != .name || forceRead else {
            orderedPhotos = PhotoDateOrdering.sorted(page.photos, by: .name, dates: dates)
            return
        }
        let pageGeneration = requestID
        let choice = sort
        let connectionID = connection.id
        // Only the listed page participates. Known camera dates need no request;
        // an incomplete preview can still gain a date from a local original.
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
                dates[result.identity] = result.date.preservingMoreComplete(dates[result.identity])
                if result.date.state == .error { dateFailures += 1 }
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
                recordDate(PhotoTakenDate(result.dateTaken, state: result.dateState), for: photo, generation: generation)
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
    let selected: Bool
    let offlineItem: OfflinePhotoItem?
    let toggleSelection: () -> Void
    let receivedDate: (PhotoTakenDate) -> Void
    let checkDate: () -> Void
    let openOriginal: () -> Void
    let keepOffline: () -> Void
    let revealOffline: () -> Void
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
                Image(systemName: offlineItem?.isVerified == true ? "checkmark.shield.fill" : (offlineItem?.hasOfflineCopy == true ? "internaldrive.fill" : "cloud.fill"))
                    .foregroundStyle(offlineItem?.isVerified == true ? moss : .secondary).padding(5)
                if loading || opening { ProgressView().controlSize(.small).frame(maxWidth: .infinity).frame(height: 110) }
            }.contentShape(Rectangle()).onTapGesture(count: 2, perform: openOriginal)
                .overlay(alignment: .topLeading) {
                    Toggle(isOn: Binding(get: { selected }, set: { _ in toggleSelection() })) { Text("Select \(photo.name)") }
                        .labelsHidden().toggleStyle(.checkbox).padding(7)
                        .accessibilityLabel("Select \(photo.name)")
                }
                .overlay { RoundedRectangle(cornerRadius: 10).stroke(selected ? moss : Color.clear, lineWidth: 2).allowsHitTesting(false) }
            Text(photo.name).font(.callout).lineLimit(1).truncationMode(.middle)
            Text(dateTaken.label).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                .frame(minHeight: 26)
                .help(dateTaken.explanation)
            if dateTaken.state == .error || dateTaken.state == .notChecked {
                Button(dateTaken.state == .error ? "Retry date" : "Check date", action: checkDate)
                    .buttonStyle(.link).font(.system(size: 10))
            }
            Text(image == nil ? (needsOriginal ? "Preview not available" : message) : "Cached preview · May be removed")
                .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            if let offlineItem {
                Text(offlineItem.stateLabel).font(.system(size: 10)).lineLimit(1)
                    .foregroundStyle(offlineItem.isVerified ? moss : .secondary)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(photo.name), \(dateTaken.label), \(offlineItem?.stateLabel ?? "Online original")")
        .accessibilityAction(named: Text("Open original"), openOriginal)
        .accessibilityAction(named: Text("Keep offline"), keepOffline)
        .help("\(photo.name) · \(ByteCountFormatter.string(fromByteCount: photo.size, countStyle: .file))\n\(dateTaken.label)\n\(message)\nDouble-click to download and open the original.")
        .contextMenu {
            Button(offlineItem?.hasOfflineCopy == true ? "Open offline copy" : "Open original", action: openOriginal)
            Button("Keep offline", action: keepOffline)
            if offlineItem?.hasOfflineCopy == true { Button("Show offline copy", action: revealOffline) }
            Button(dateTaken.state == .error ? "Retry date" : "Check date", action: checkDate)
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
            receivedDate(PhotoTakenDate(result.dateTaken, state: result.dateState))
            if let path = result.thumbnailPath { image = NSImage(contentsOfFile: path) }
            needsOriginal = result.needsOriginal ?? false
            message = result.message ?? (image == nil ? "Online only" : "Small preview cached")
        } catch {
            if !Task.isCancelled && loadID == generation {
                message = error.localizedDescription
                receivedDate(PhotoTakenDate(nil, state: "error"))
            }
        }
        if loadID == generation { loading = false }
    }
}
