import AppKit
import Darwin
import FinderSync

// The extension only asks the local service about items Finder has displayed.
// It never opens the network volume or holds AWS credentials.
private struct Bridge: Decodable {
    let version: Int
    let url: String
    let token: String

    var endpoint: URL? {
        guard version == 1, token.count >= 32, token.count <= 512,
              token.utf8.allSatisfy({ (65...90).contains($0) || (97...122).contains($0)
                  || (48...57).contains($0) || $0 == 45 || $0 == 95 }),
              let endpoint = URL(string: url), endpoint.scheme == "http",
              endpoint.host == "127.0.0.1", let port = endpoint.port,
              (1024...65535).contains(port), endpoint.user == nil,
              endpoint.password == nil, endpoint.query == nil, endpoint.fragment == nil,
              endpoint.path.isEmpty || endpoint.path == "/" else { return nil }
        return endpoint
    }
}

private struct Root: Decodable {
    let id: String
    let name: String
    let mountPath: String
    let state: String
    let mounted: Bool
}

private struct RootsResponse: Decodable { let version: Int; let roots: [Root] }
private struct BadgeResponse: Decodable {
    struct Item: Decodable { let path: String; let state: String }
    let badges: [Item]
}

private final class LocalOnlySessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        // The bridge has no redirects. Never forward its token to another URL.
        completionHandler(nil)
    }
}

private enum Badge: String, CaseIterable {
    case online, cached, partial, pending, error, unknown

    var label: String {
        switch self {
        case .online: return "Online only"
        case .cached: return "Cached on this Mac"
        case .partial: return "Partially cached"
        case .pending: return "Waiting to upload"
        case .error: return "Needs attention"
        case .unknown: return "Status unavailable"
        }
    }

    var color: NSColor {
        switch self {
        case .cached: return NSColor(srgbRed: 0.04, green: 0.70, blue: 0.38, alpha: 1)
        case .pending: return NSColor(srgbRed: 0.12, green: 0.58, blue: 0.91, alpha: 1)
        case .partial: return NSColor(srgbRed: 0.38, green: 0.52, blue: 0.66, alpha: 1)
        case .online: return NSColor(srgbRed: 0.48, green: 0.62, blue: 0.68, alpha: 1)
        case .error: return NSColor(srgbRed: 0.88, green: 0.25, blue: 0.25, alpha: 1)
        case .unknown: return NSColor(srgbRed: 0.55, green: 0.58, blue: 0.61, alpha: 1)
        }
    }

    var symbol: String {
        switch self {
        case .online: return "cloud"
        case .cached: return "checkmark"
        case .partial: return "circle.lefthalf.filled"
        case .pending: return "arrow.triangle.2.circlepath"
        case .error: return "exclamationmark"
        case .unknown: return "questionmark"
        }
    }

    var image: NSImage {
        // Draw original badges at the largest size Finder requests; no outer padding.
        let result = NSImage(size: NSSize(width: 320, height: 320), flipped: false) { rect in
            let circle = NSBezierPath(ovalIn: rect.insetBy(dx: 7, dy: 7))
            (self == .cached ? NSColor.white : self.color).setFill()
            circle.fill()
            (self == .cached ? self.color : NSColor.white).setStroke()
            circle.lineWidth = 14
            circle.stroke()
            let configuration = NSImage.SymbolConfiguration(pointSize: 178, weight: .bold)
            if let symbol = NSImage(systemSymbolName: self.symbol, accessibilityDescription: self.label)?
                .withSymbolConfiguration(configuration) {
                let symbolRect = NSRect(x: 65, y: 65, width: 190, height: 190)
                let tinted = NSImage(size: symbol.size)
                tinted.lockFocus()
                symbol.draw(at: .zero, from: .zero, operation: .sourceOver, fraction: 1)
                (self == .cached ? self.color : NSColor.white).setFill()
                NSRect(origin: .zero, size: symbol.size).fill(using: .sourceAtop)
                tinted.unlockFocus()
                tinted.draw(in: symbolRect, from: .zero, operation: .sourceOver, fraction: 1,
                            respectFlipped: false, hints: [.interpolation: NSImageInterpolation.high])
            }
            return true
        }
        result.isTemplate = false
        return result
    }
}

@objc(MountainTurtleFinderSync)
final class MountainTurtleFinderSync: FIFinderSync {
    private let controller = FIFinderSyncController.default()
    private let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        configuration.timeoutIntervalForResource = 4
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.connectionProxyDictionary = [:]
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration, delegate: LocalOnlySessionDelegate(), delegateQueue: nil)
    }()
    private var roots: [Root] = []
    private var observed = Set<String>()
    private var requested: [String: URL] = [:]
    private var requestOrder: [String] = []
    private var currentBadges: [String: Badge] = [:]
    private var bridge: Bridge?
    private var timer: Timer?
    private var refreshing = false
    private var refreshScheduled = false
    private var rootsChecked = Date.distantPast
    private var lastSuccessfulBadges = Date.distantPast
    private let maxTrackedItems = 4096

    override init() {
        super.init()
        controller.directoryURLs = []
        for badge in Badge.allCases {
            controller.setBadgeImage(badge.image, label: badge.label, forBadgeIdentifier: badge.rawValue)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
    }

    override func beginObservingDirectory(at url: URL) {
        observed.insert(url.standardizedFileURL.path)
        scheduleRefresh()
    }

    override func endObservingDirectory(at url: URL) {
        let ended = url.standardizedFileURL.path
        observed.remove(ended)
        for path in Array(requested.keys) where contains(path, in: ended) {
            if !observed.contains(where: { contains(path, in: $0) }) {
                requested.removeValue(forKey: path)
                currentBadges.removeValue(forKey: path)
            }
        }
        requestOrder.removeAll { requested[$0] == nil }
    }

    override func requestBadgeIdentifier(for url: URL) {
        guard url.isFileURL else { return }
        let normalized = url.standardizedFileURL
        let path = normalized.path
        guard roots.contains(where: { contains(path, in: $0.mountPath) }) else { return }
        if requested[path] == nil { requestOrder.append(path) }
        requested[path] = normalized
        while requestOrder.count > maxTrackedItems {
            let oldest = requestOrder.removeFirst()
            if let discarded = requested.removeValue(forKey: oldest) {
                // Finder can retain an assigned badge after we stop tracking an
                // item. Clear old green checks when bounded tracking evicts it.
                controller.setBadgeIdentifier(Badge.unknown.rawValue, for: discarded)
            }
            currentBadges.removeValue(forKey: oldest)
        }
        let badge = Date().timeIntervalSince(lastSuccessfulBadges) < 10 ? currentBadges[path] ?? .unknown : .unknown
        controller.setBadgeIdentifier(badge.rawValue, for: normalized)
        scheduleRefresh()
    }

    override func menu(for menuKind: FIMenuKind) -> NSMenu? {
        guard menuKind == .contextualMenuForItems,
              let selected = controller.selectedItemURLs(), selected.count == 1,
              let url = selected.first,
              roots.contains(where: { contains(url.standardizedFileURL.path, in: $0.mountPath) }) else { return nil }
        let badge = Date().timeIntervalSince(lastSuccessfulBadges) < 10
            ? currentBadges[url.standardizedFileURL.path] ?? .unknown : .unknown
        let menu = NSMenu()
        let item = NSMenuItem(title: "Mountain Turtle: \(badge.label)", action: nil, keyEquivalent: "")
        item.image = badge.image
        item.isEnabled = false
        menu.addItem(item)
        return menu
    }

    private func contains(_ path: String, in directory: String) -> Bool {
        let root = directory.hasSuffix("/") ? String(directory.dropLast()) : directory
        return path == root || path.hasPrefix(root + "/")
    }

    private func scheduleRefresh() {
        guard !refreshScheduled else { return }
        refreshScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.10) { [weak self] in
            self?.refreshScheduled = false
            self?.refresh()
        }
    }

    private func readBridge() -> Bridge? {
        // NSHomeDirectory is the sandbox container in an extension. passwd gives
        // the real home; only this dedicated, read-only directory is entitled.
        guard let entry = getpwuid(getuid()), let home = entry.pointee.pw_dir else { return nil }
        let location = URL(fileURLWithPath: String(cString: home), isDirectory: true)
            .appendingPathComponent("Library/Application Support/Mountain Turtle/Finder/bridge.json")
        let descriptorFD = Darwin.open(location.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK)
        guard descriptorFD >= 0 else { return nil }
        defer { Darwin.close(descriptorFD) }
        var info = stat()
        guard fstat(descriptorFD, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == geteuid(),
              (info.st_mode & 0o077) == 0, info.st_size > 0, info.st_size <= 4096 else { return nil }
        var buffer = [UInt8](repeating: 0, count: 4097)
        var size = 0
        while size < buffer.count {
            let count = buffer.withUnsafeMutableBytes { bytes in
                Darwin.read(descriptorFD, bytes.baseAddress!.advanced(by: size), bytes.count - size)
            }
            if count == 0 { break }
            if count < 0 {
                if errno == EINTR { continue }
                return nil
            }
            size += count
        }
        guard size > 0, size <= 4096 else { return nil }
        let data = Data(buffer.prefix(size))
        guard
              let descriptor = try? JSONDecoder().decode(Bridge.self, from: data),
              descriptor.endpoint != nil else { return nil }
        return descriptor
    }

    private func refresh() {
        guard !refreshing else {
            if Date().timeIntervalSince(lastSuccessfulBadges) >= 10 { markUnavailable() }
            return
        }
        guard let descriptor = readBridge() else { markUnavailable(); return }
        let changed = bridge?.url != descriptor.url || bridge?.token != descriptor.token
        bridge = descriptor
        refreshing = true
        if changed || Date().timeIntervalSince(rootsChecked) >= 10 {
            request(path: "v1/roots", descriptor: descriptor) { [weak self] data in
                guard let self else { return }
                guard let data, let response = try? JSONDecoder().decode(RootsResponse.self, from: data),
                      response.version == 1 else { self.markUnavailable(); self.refreshing = false; return }
                self.rootsChecked = Date()
                self.roots = response.roots.filter { $0.mountPath.hasPrefix("/") && $0.mountPath != "/" }
                let urls = Set(self.roots.map { URL(fileURLWithPath: $0.mountPath, isDirectory: true) })
                if urls != self.controller.directoryURLs { self.controller.directoryURLs = urls }
                let removed = self.requested.keys.filter { path in
                    !self.roots.contains(where: { self.contains(path, in: $0.mountPath) })
                }
                for path in removed {
                    if let url = self.requested.removeValue(forKey: path) {
                        self.controller.setBadgeIdentifier("", for: url)
                    }
                    self.currentBadges.removeValue(forKey: path)
                }
                self.requestOrder.removeAll { self.requested[$0] == nil }
                self.refreshBadges(descriptor: descriptor)
            }
        } else {
            refreshBadges(descriptor: descriptor)
        }
    }

    private func refreshBadges(descriptor: Bridge) {
        let paths = requestOrder.filter { requested[$0] != nil }
        guard !paths.isEmpty else { refreshing = false; return }
        fetchBatch(paths: paths, offset: 0, descriptor: descriptor)
    }

    private func fetchBatch(paths: [String], offset: Int, descriptor: Bridge) {
        guard offset < paths.count else {
            lastSuccessfulBadges = Date()
            refreshing = false
            return
        }
        var batch: [String] = []
        // S3 keys can be long and contain Unicode. Keep both the item count and
        // encoded body under the bridge's limits, rather than counting paths alone.
        for path in paths[offset..<min(paths.count, offset + 128)] {
            let candidate = batch + [path]
            guard let encoded = try? JSONSerialization.data(withJSONObject: ["paths": candidate]),
                  encoded.count <= 60_000 else { break }
            batch = candidate
        }
        guard !batch.isEmpty else {
            let path = paths[offset]
            if let url = requested[path] {
                controller.setBadgeIdentifier(Badge.unknown.rawValue, for: url)
                currentBadges[path] = .unknown
            }
            fetchBatch(paths: paths, offset: offset + 1, descriptor: descriptor)
            return
        }
        guard let body = try? JSONSerialization.data(withJSONObject: ["paths": batch]) else {
            markUnavailable(); refreshing = false; return
        }
        request(path: "v1/badges", descriptor: descriptor, body: body) { [weak self] data in
            guard let self else { return }
            guard let data, let response = try? JSONDecoder().decode(BadgeResponse.self, from: data) else {
                self.markUnavailable(); self.refreshing = false; return
            }
            var states: [String: Badge] = [:]
            let expected = Set(batch)
            for item in response.badges where expected.contains(item.path) {
                states[item.path] = Badge(rawValue: item.state) ?? .unknown
            }
            for path in batch {
                guard let url = self.requested[path] else { continue }
                let badge = states[path] ?? .unknown
                if self.currentBadges[path] != badge {
                    self.controller.setBadgeIdentifier(badge.rawValue, for: url)
                    self.currentBadges[path] = badge
                }
            }
            self.fetchBatch(paths: paths, offset: offset + batch.count, descriptor: descriptor)
        }
    }

    private func markUnavailable() {
        for (path, url) in requested where currentBadges[path] != .unknown {
            controller.setBadgeIdentifier(Badge.unknown.rawValue, for: url)
            currentBadges[path] = .unknown
        }
        lastSuccessfulBadges = .distantPast
    }

    private func request(path: String, descriptor: Bridge, body: Data? = nil,
                         completion: @escaping (Data?) -> Void) {
        guard let endpoint = descriptor.endpoint else { completion(nil); return }
        var request = URLRequest(url: endpoint.appendingPathComponent(path))
        request.setValue("Bearer " + descriptor.token, forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        session.dataTask(with: request) { data, response, _ in
            let success = (response as? HTTPURLResponse)?.statusCode == 200
            let result = success && (data?.count ?? 0) <= 2_000_000 ? data : nil
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }
}
