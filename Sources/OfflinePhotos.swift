import Foundation

struct OfflinePhotoItem: Decodable, Identifiable, Sendable {
    let id: String
    let key: String
    let name: String
    let etag: String
    let size: Int64
    let state: String
    let bytesDownloaded: Int64
    let path: String?
    let error: String?
    let sha256: String?
    let verification: String?
    let verifiedAt: Double?

    var identity: PhotoIdentity { PhotoIdentity(key: key, etag: etag, size: size) }
    var hasOfflineCopy: Bool { ["downloaded", "verified"].contains(state) && path != nil }
    var isVerified: Bool { state == "verified" && hasOfflineCopy }
    var isPending: Bool { ["queued", "downloading", "paused"].contains(state) }
    var progress: Double { size > 0 ? min(1, max(0, Double(bytesDownloaded) / Double(size))) : 0 }
    var progressLabel: String {
        "\(ByteCountFormatter.string(fromByteCount: max(0, min(size, bytesDownloaded)), countStyle: .file)) of \(ByteCountFormatter.string(fromByteCount: size, countStyle: .file))"
    }
    var stateLabel: String {
        switch state {
        case "queued": return "Queued"
        case "downloading": return "Downloading"
        case "paused": return "Paused"
        case "downloaded": return hasOfflineCopy ? "Kept offline · Not verified" : "Offline copy unavailable"
        case "verified": return hasOfflineCopy ? "Kept offline · Verified" : "Offline copy unavailable"
        case "error": return "Needs attention"
        default: return "Checking download"
        }
    }
}

struct OfflinePhotoQueue: Decodable, Sendable {
    let paused: Bool
    let workerRunning: Bool
    let items: [OfflinePhotoItem]

    var pendingCount: Int { items.filter(\.isPending).count }
    var verifiedCount: Int { items.filter(\.isVerified).count }
    var downloadedCount: Int { items.filter(\.hasOfflineCopy).count }
    var failedCount: Int { items.filter { $0.state == "error" }.count }
    var summary: String {
        if items.isEmpty { return "Keep selected photos offline" }
        var parts = ["\(downloadedCount) kept offline"]
        if pendingCount > 0 { parts.append("\(pendingCount) \(paused ? (workerRunning ? "pausing…" : "paused") : "remaining")") }
        if failedCount > 0 { parts.append("\(failedCount) need attention") }
        return parts.joined(separator: " · ")
    }

    func item(for photo: PhotoItem) -> OfflinePhotoItem? { items.first { $0.identity == photo.identity } }
}
