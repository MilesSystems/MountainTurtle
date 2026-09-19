import SwiftUI

private struct S3Details: Decodable {
    var bucketType: String
    var storageClass: String
    var message: String?
    var sourceTimestamp: Double?

    var storageHelp: String {
        var text = message ?? "Storage classes reported by AWS daily storage metrics."
        if let sourceTimestamp {
            text += " Reported " + Date(timeIntervalSince1970: sourceTimestamp).formatted(date: .abbreviated, time: .omitted) + "."
        }
        return text
    }
}

struct S3DetailsRows: View {
    let connection: Connection
    @State private var details: S3Details?
    @State private var loading = false
    @State private var failure: String?

    // A connection edit must not keep details from the old bucket or account.
    private var identity: String { [connection.id, connection.bucket, connection.profile, connection.region].joined(separator: "\n") }

    var body: some View {
        VStack(spacing: 0) {
            row("S3 type", value: details?.bucketType ?? (failure == nil ? "Checking…" : "Unavailable"), symbol: "shippingbox")
            Divider().padding(.leading, 42)
            HStack(spacing: 12) {
                row("Storage class", value: details?.storageClass ?? (loading ? "Checking…" : "Unavailable"), symbol: "square.stack.3d.up")
                    .help(failure ?? details?.storageHelp ?? "Checking AWS daily storage metrics.")
                Button { Task { await load(refresh: true) } } label: {
                    Image(systemName: "arrow.clockwise")
                }.buttonStyle(.plain).foregroundStyle(moss)
                    .accessibilityLabel("Refresh S3 details").help("Refresh S3 type and storage class")
                    .disabled(loading)
            }
            Divider().padding(.leading, 42)
            S3CostSummaryRows(connection: connection)
        }.task(id: identity) {
            details = nil; failure = nil
            await load(refresh: false)
        }
    }

    private func row(_ label: String, value: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 17).foregroundStyle(moss.opacity(0.8))
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(value).frame(maxWidth: .infinity, alignment: .trailing)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 12)).padding(.vertical, 13)
    }

    @MainActor private func load(refresh: Bool) async {
        let requestedIdentity = identity
        loading = true; failure = nil
        defer { if !Task.isCancelled { loading = false } }
        do {
            var arguments = ["details", connection.id]
            if refresh { arguments.append("--refresh") }
            let data = try await ServiceClient.run(arguments, scriptName: "s3_details.py")
            try Task.checkCancellation()
            guard requestedIdentity == identity else { return }
            details = try JSONDecoder().decode(S3Details.self, from: data)
        } catch is CancellationError { return }
        catch {
            guard !Task.isCancelled, requestedIdentity == identity else { return }
            failure = error.localizedDescription
        }
    }
}
