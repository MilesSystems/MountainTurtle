import SwiftUI

private struct ObservedOperation: Decodable, Identifiable {
    let id: String
    let kind: String
    let state: String
    let title: String
    let path: String
    let startedAt: Double
    let updatedAt: Double
    let durationSeconds: Double?
    let observed: Bool?
    let bytes: Double?
    let size: Double?
    let speed: Double?

    var symbol: String {
        switch kind {
        case "list": return "folder"
        case "preview": return "photo"
        case "metadata": return "info.circle"
        default: return "arrow.down.circle"
        }
    }
    func timing(_ now: Date) -> String {
        if state == "running" {
            let seconds = max(0, now.timeIntervalSince1970 - startedAt)
            return (observed == true ? "Running · observed for " : "Running · elapsed ") + duration(seconds)
                + (seconds >= 15 && kind == "list" ? " · waiting for listing" : "")
        }
        let outcome = ["complete": "Done", "failed": "Failed", "cancelled": "Cancelled", "paused": "Paused", "unavailable": "Preview unavailable"][state] ?? "Outcome not confirmed"
        guard let durationSeconds, state != "unknown" else { return outcome }
        return outcome + " · " + duration(durationSeconds)
    }
    private func duration(_ seconds: Double) -> String {
        if seconds < 1 { return String(format: "%.0f ms", seconds * 1000) }
        if seconds < 60 { return String(format: "%.1f s", seconds) }
        return String(format: "%.0f min %.0f s", floor(seconds / 60), seconds.truncatingRemainder(dividingBy: 60))
    }
}

private struct OperationSnapshot: Decodable {
    let events: [ObservedOperation]
    let message: String
}

struct OperationActivityView: View {
    let connection: Connection
    @State private var snapshot: OperationSnapshot?
    @State private var lastRead = Date.distantPast
    @State private var unavailable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Activity queue & history", systemImage: "waveform.path").font(.headline)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let stale = unavailable || context.date.timeIntervalSince(lastRead) > 12
                VStack(alignment: .leading, spacing: 10) {
                    if snapshot?.events.isEmpty == true {
                        Text("No captured requests yet. Browse a folder or request a preview to see activity here.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    ForEach(Array((snapshot?.events ?? []).prefix(8))) { event in
                        HStack(alignment: .top, spacing: 10) {
                            if event.state == "running" && !stale {
                                ProgressView().controlSize(.small).frame(width: 20)
                            } else {
                                Image(systemName: event.symbol).frame(width: 20).foregroundStyle(moss)
                            }
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(event.title).font(.system(size: 12, weight: .semibold))
                                    Spacer()
                                    Text(Date(timeIntervalSince1970: event.updatedAt), format: .dateTime.hour().minute().second())
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Text(event.path.isEmpty ? "/" : event.path).font(.caption).foregroundStyle(.secondary)
                                    .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                                Text(stale && event.state == "running" ? "Live status unavailable" : event.timing(context.date))
                                    .font(.caption).foregroundStyle(event.state == "failed" ? .orange : moss)
                                if let bytes = event.bytes, bytes.isFinite, bytes >= 0, bytes < Double(Int64.max) {
                                    Text(ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
                                         + (event.speed.map { " · " + String(format: "%.0f KB/s", $0 / 1000) } ?? ""))
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    if stale { Text("Live activity is not currently confirmed.").font(.caption).foregroundStyle(.secondary) }
                }
            }
            Text(snapshot?.message ?? "Checking live transfers and recent app requests…")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .task(id: connection.id) {
            snapshot = nil
            while !Task.isCancelled {
                do {
                    let data = try await ServiceClient.run(["operation-status", connection.id])
                    let result = try JSONDecoder().decode(OperationSnapshot.self, from: data)
                    guard !Task.isCancelled else { break }
                    snapshot = result
                    lastRead = Date()
                    unavailable = false
                } catch { unavailable = true }
                do { try await Task.sleep(nanoseconds: 3_000_000_000) } catch { break }
            }
        }
    }
}
