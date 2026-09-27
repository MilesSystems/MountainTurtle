import SwiftUI

private struct UploadSnapshot: Decodable {
    var connectionID: String
    var checkedAt: Double
    var state: String
    var title: String
    var message: String
    var canRetry: Bool
}

struct UploadStatusView: View {
    @ObservedObject var model: AppModel
    let connection: Connection
    @State private var snapshot: UploadSnapshot?
    @State private var retryMessage: String?

    private var current: UploadSnapshot? {
        guard let snapshot, snapshot.connectionID == connection.id,
              Date().timeIntervalSince1970 - snapshot.checkedAt < 15 else { return nil }
        return snapshot
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(current?.title ?? "Checking upload status…",
                  systemImage: current?.state == "clear" ? "checkmark.circle" : "arrow.up.circle")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(current?.state == "clear" ? moss : .secondary)
            Text(current?.message ?? "Upload completion has not been confirmed.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let last = connection.events?.filter({ $0.state == "complete" && ["upload", "download"].contains($0.kind) })
                .max(by: { $0.updatedAt < $1.updatedAt }) {
                Text("Last recorded transfer: \(last.timeLabel)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if current?.canRetry == true {
                Button("Retry queued uploads") {
                    Task {
                        if await model.action(["retry-uploads", connection.id]) {
                            retryMessage = "Retry requested. Uploads resume when the server is reachable; keep the drive connected."
                            await refresh()
                        }
                    }
                }.disabled(model.activeAction != nil)
            }
            if let retryMessage { Text(retryMessage).font(.caption).foregroundStyle(.secondary) }
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(cream).clipShape(RoundedRectangle(cornerRadius: 14))
        .task(id: connection.id) {
            snapshot = nil
            retryMessage = nil
            while !Task.isCancelled {
                await refresh()
                do { try await Task.sleep(nanoseconds: 3_000_000_000) }
                catch { break }
            }
        }
    }

    @MainActor private func refresh() async {
        do {
            let data = try await ServiceClient.run(["upload-status", connection.id])
            let result = try JSONDecoder().decode(UploadSnapshot.self, from: data)
            guard !Task.isCancelled else { return }
            snapshot = result
        } catch {
            guard !Task.isCancelled else { return }
            snapshot = UploadSnapshot(connectionID: connection.id, checkedAt: Date().timeIntervalSince1970,
                state: "unknown", title: "Upload status unavailable",
                message: "Could not check uploads. Cached changes are preserved; refresh or reconnect when available.", canRetry: false)
        }
    }
}
