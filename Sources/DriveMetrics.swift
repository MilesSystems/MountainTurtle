import SwiftUI
import Charts
import AppKit

private final class MetricsProcess: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    func start(_ process: Process) throws {
        lock.lock(); defer { lock.unlock() }
        if cancelled { throw CancellationError() }
        try process.run(); self.process = process
    }
    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

private enum MetricsClient {
    static func run<T: Decodable>(_ arguments: [String], script: String = "drive_metrics.py", as type: T.Type) async throws -> T {
        let handle = MetricsProcess()
        let data: Data = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        guard let python = ServiceClient.pythonPath else { throw TurtleError(message: "Python is unavailable.") }
                        let process = Process(), output = Pipe()
                        process.executableURL = URL(fileURLWithPath: python)
                        process.arguments = ["-B", ServiceClient.resources.appendingPathComponent("service/\(script)").path,
                                             "--resource-dir", ServiceClient.resources.path] + arguments
                        process.standardOutput = output; process.standardError = FileHandle.nullDevice
                        process.standardInput = FileHandle.nullDevice
                        try handle.start(process)
                        let data = output.fileHandleForReading.readDataToEndOfFile()
                        process.waitUntilExit()
                        if process.terminationStatus != 0 {
                            let response = try? JSONDecoder().decode(ActionResponse.self, from: data)
                            throw TurtleError(message: response?.error ?? "Drive metrics could not be read. Try refreshing.")
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

private struct DriveMetricPoint: Decodable {
    var timestamp: Double
    var transferredBytes: Double?
    var speedBytesPerSecond: Double?
    var averageSpeedBytesPerSecond: Double?
    var transferCount: Double?
    var checks: Double?
    var errors: Double?
    var activeTransfers: Double?
    var elapsedSeconds: Double?
    var cacheBytes: Double?
    var cacheFiles: Double?
    var cacheLimitBytes: Double?
    var cacheInUse: Double?
    var cacheOutOfSpace: Bool?
    var uploadsQueued: Double?
    var uploadsInProgress: Double?
    var sessionID: String?
    var cacheErrors: Double?
}
private struct DriveMetricSnapshot: Decodable {
    var ok: Bool
    var timestamp: Double
    var status: String
    var message: String?
    var current: DriveMetricPoint?
    var history: [DriveMetricPoint]
}
private struct StorageClassMetric: Decodable, Identifiable {
    var name: String
    var bytes: Double
    var id: String { name }
    var label: String {
        name.replacingOccurrences(of: "Storage", with: "")
            .replacingOccurrences(of: "IntelligentTiering", with: "Intelligent Tiering ")
            .replacingOccurrences(of: "Glacier", with: "Glacier ")
    }
}
private struct StorageHistoryMetric: Decodable {
    var timestamp: Double
    var totalBytes: Double?
    var objectCount: Double?
    var complete: Bool?
    var usedBytes: Double?
    var freeBytes: Double?
}
private struct StoragePriceComponent: Decodable, Identifiable {
    var storageType: String
    var bytes: Double
    var monthlyUSD: Double?
    var sku: String?
    var effectiveRateUSDPerGiBMonth: Double?
    var id: String { storageType }
    var label: String { StorageClassMetric(name: storageType, bytes: bytes).label }
}
private struct StorageCostEstimate: Decodable {
    var method: String?
    var monthlyUSD: Double?
    var knownMonthlyUSD: Double?
    var annualUSD: Double?
    var status: String?
    var pricingFetchedAt: Double?
    var pricingPublishedAt: Double?
    var region: String?
    var sourceURL: String?
    var assumptions: [String]?
    var unsupportedStorageTypes: [String]?
    var components: [StoragePriceComponent]?
    var message: String?
}
private struct DriveStorageSnapshot: Decodable {
    var provider: String?
    var source: String?
    var scope: String?
    var usedBytes: Double?
    var freeBytes: Double?
    var ok: Bool
    var queriedAt: Double?
    var status: String
    var message: String?
    var sourceTimestamp: Double?
    var totalBytes: Double?
    var objectCount: Double?
    var storageClasses: [StorageClassMetric]?
    var history: [StorageHistoryMetric]?
    var assumptions: [String]?
    var isStale: Bool?
    var missingStorageTypes: [String]?
    var unreportedStorageTypes: [String]?
    var estimate: StorageCostEstimate?
}
private struct BucketActivityPoint: Decodable {
    var timestamp: Double
    var uploadedBytes: Double?
    var downloadedBytes: Double?
    var uploadBytesPerSecond: Double?
    var downloadBytesPerSecond: Double?
    var putRequests: Double?
    var getRequests: Double?
    var allRequests: Double?
    var clientErrors: Double?
    var serverErrors: Double?
    var firstByteLatencyMs: Double?
    var totalRequestLatencyMs: Double?
}
private struct BucketActivitySummary: Decodable {
    var uploadedBytes: Double?
    var downloadedBytes: Double?
    var putRequests: Double?
    var getRequests: Double?
    var allRequests: Double?
    var clientErrors: Double?
    var serverErrors: Double?
}
private struct BucketActivityCoverage: Decodable {
    var expectedMinutes: Double?
    var observedMinutes: Double?
    var uploadedMinutes: Double?
    var downloadedMinutes: Double?
}
private struct BucketActivitySnapshot: Decodable {
    var ok: Bool
    var source: String?
    var scope: String?
    var queriedAt: Double?
    var windowHours: Int?
    var periodSeconds: Double?
    var windowStart: Double?
    var windowEnd: Double?
    var filterID: String?
    var status: String
    var message: String?
    var sourceTimestamp: Double?
    var lagSeconds: Double?
    var current: BucketActivityPoint?
    var summary: BucketActivitySummary?
    var coverage: BucketActivityCoverage?
    var history: [BucketActivityPoint]?
    var assumptions: [String]?
}
private struct AWSBillingDay: Decodable, Identifiable {
    var date: String
    var timestamp: Double
    var amount: Double
    var estimated: Bool
    var id: String { date }
}
private struct AWSBillingSnapshot: Decodable {
    var ok: Bool
    var source: String?
    var scope: String?
    var accountID: String?
    var currency: String?
    var monthStart: String?
    var periodEnd: String?
    var queriedAt: Double?
    var cached: Bool?
    var status: String
    var total: Double?
    var estimated: Bool?
    var history: [AWSBillingDay]?
    var message: String?
    var assumptions: [String]?
}
private struct MetricChartPoint: Identifiable {
    let id: String
    let date: Date
    let value: Double
    let series: String
    var category: String? = nil
}
private struct CostScenarioPoint: Identifiable {
    let month: Int
    let monthlyUSD: Double
    var id: Int { month }
}

private let metricsBlue = Color(red: 0.27, green: 0.49, blue: 0.62)
private let metricsGold = Color(red: 0.69, green: 0.48, blue: 0.22)
private let metricsColors: [Color] = [moss, metricsBlue, metricsGold, .purple, .mint, .orange, .indigo, .gray]

struct DriveMetricsView: View {
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var snapshot: DriveMetricSnapshot?
    @State private var storage: DriveStorageSnapshot?
    @State private var snapshotFailure: String?
    @State private var storageFailure: String?
    @State private var storageLoading = false
    @State private var storageRequestID = UUID()
    @State private var rateText = ""
    @State private var monthlyGrowth = 0.0
    @State private var historyMinutes = 60
    @State private var activity: BucketActivitySnapshot?
    @State private var activityFailure: String?
    @State private var activityLoading = false
    @State private var activityHours = 1
    @State private var activityRequestID = UUID()
    @State private var billing: AWSBillingSnapshot?
    @State private var billingFailure: String?
    @State private var billingLoading = false
    @State private var billingRequestID = UUID()

    private var current: DriveMetricPoint? { snapshot?.current }
    private var rateKey: String { "storageRateUSDPerGiBMonth.\(connection.id)" }
    private var rate: Double? {
        guard let value = Double(rateText.trimmingCharacters(in: .whitespacesAndNewlines)), value.isFinite, value >= 0, value <= 1_000_000 else { return nil }
        return value
    }
    private var manualRateEntered: Bool { !rateText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private var monthlyCost: Double? {
        if manualRateEntered {
            guard let bytes = storage?.totalBytes, let rate else { return nil }
            let cost = bytes / 1_073_741_824 * rate
            return cost.isFinite ? cost : nil
        }
        guard storage?.estimate?.status == "available", let cost = storage?.estimate?.monthlyUSD, cost.isFinite, cost >= 0 else { return nil }
        return cost
    }
    private var estimateMethod: String {
        manualRateEntered ? "Your blended rate" : "Public AWS pricing · \(storage?.estimate?.region ?? connection.region)"
    }
    private var estimateDetail: String {
        if manualRateEntered && rate == nil { return "Enter a valid rate below" }
        if storage?.isStale == true { return "Estimate uses older storage data" }
        if storage?.status == "partial" { return "Estimate uses last complete data" }
        return manualRateEntered ? "Estimate at your blended rate" : monthlyCost == nil ? "Automatic estimate unavailable" : "Estimate at public AWS rates"
    }
    private var scenarioUnavailable: String {
        if storage?.totalBytes == nil { return "A complete storage measurement is needed for estimates." }
        if manualRateEntered { return "Enter a valid blended storage rate to see an estimate." }
        return "Automatic pricing is unavailable for this storage. Enter a blended rate to estimate it."
    }
    private var scenario: [CostScenarioPoint] {
        guard let cost = monthlyCost else { return [] }
        return (1...12).map { CostScenarioPoint(month: $0, monthlyUSD: cost * pow(1 + monthlyGrowth / 100, Double($0 - 1))) }
    }
    private var classes: [StorageClassMetric] { (storage?.storageClasses ?? []).filter { $0.bytes > 0 }.sorted { $0.bytes > $1.bytes } }
    private var storageHistory: [StorageHistoryMetric] { (storage?.history ?? []).sorted { $0.timestamp < $1.timestamp } }
    private var currentHistory: [DriveMetricPoint] {
        let cutoff = (snapshot?.timestamp ?? Date().timeIntervalSince1970) - Double(historyMinutes * 60)
        return (snapshot?.history ?? []).filter { $0.timestamp >= cutoff }.sorted { $0.timestamp < $1.timestamp }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if connection.isSFTP {
                        sftpStorageSection
                        liveSection
                    } else {
                        remoteActivitySection
                        cloudSection
                        awsPricingSection
                        actualSpendSection
                        DisclosureGroup("Explore a scenario") { costSection.padding(.top, 12) }.font(.callout.weight(.medium))
                        DisclosureGroup("This Mac: transfer & cache") { liveSection.padding(.top, 12) }.font(.callout.weight(.medium))
                    }
                    Text("Local observations refresh every 5 seconds while this dashboard is open. Remote storage refreshes on opening and manually." + (connection.isSFTP ? "" : " Configured AWS request metrics refresh every minute while open; AWS publication can lag."))
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }.padding(24)
            }.background(cream.opacity(0.6))
        }.frame(width: 930, height: 740).tint(moss)
            .task {
                rateText = UserDefaults.standard.string(forKey: rateKey) ?? ""
                while !Task.isCancelled {
                    await refreshSnapshot()
                    do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { break }
                }
            }
            .task(id: storageRequestID) { await refreshStorage() }
            .task(id: activityRequestID) {
                guard !connection.isSFTP else { return }
                while !Task.isCancelled {
                    await refreshActivity()
                    guard activity?.filterID != nil,
                          !["notConfigured", "configurationIncomplete", "permissionDenied", "authenticationRequired", "unsupported"].contains(activity?.status ?? "") else { break }
                    do { try await Task.sleep(nanoseconds: 60_000_000_000) } catch { break }
                }
            }
            .task(id: billingRequestID) { if !connection.isSFTP { await refreshBilling() } }
            .onChange(of: activityHours) { _, _ in activity = nil; activityRequestID = UUID() }
            .onChange(of: rateText) { _, value in
                if value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { UserDefaults.standard.removeObject(forKey: rateKey) }
                else if rate != nil { UserDefaults.standard.set(value, forKey: rateKey) }
            }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "chart.xyaxis.line").font(.system(size: 27, weight: .medium)).foregroundStyle(moss)
                .frame(width: 52, height: 52).background(cream).clipShape(RoundedRectangle(cornerRadius: 14))
            VStack(alignment: .leading, spacing: 4) {
                Text("Drive insights").font(.system(size: 24, weight: .semibold)).foregroundStyle(ink)
                Text("\(connection.name) · \(connection.isSFTP ? "SFTP" : "Amazon S3")").font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if connection.isSFTP {
                if snapshot == nil && snapshotFailure == nil { ProgressView().controlSize(.small) }
                statusPill(snapshotFailure != nil ? "Last reading" : snapshot?.status == "available" ? "Live" : snapshot?.status == "partial" ? "Partial data" : snapshot?.status == "disconnected" ? "Disconnected" : "Waiting for data",
                           color: snapshot?.status == "available" && snapshotFailure == nil ? moss : .secondary)
            } else {
                if activityLoading { ProgressView().controlSize(.small) }
                statusPill("AWS remote metrics", color: moss)
            }
            Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
        }.padding(22)
    }

    private var remoteActivitySection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                sectionHeading("Bucket activity · all computers", subtitle: "AWS request metrics for \(connection.bucket)", symbol: "network")
                Spacer()
                Picker("Remote activity range", selection: $activityHours) {
                    Text("1 hour").tag(1); Text("6 hours").tag(6); Text("24 hours").tag(24)
                }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Remote activity range").frame(width: 240)
                Button { activityRequestID = UUID() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Refresh bucket activity").accessibilityLabel("Refresh bucket activity").disabled(activityLoading)
            }
            Text("Uploads from your other computer appear here when AWS publishes the bucket's request metrics. This view does not depend on this Mac reading the files or keeping the drive mounted.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let failure = activityFailure { message(failure, color: .orange) }
            if let activity, activity.status != "available" {
                message(activity.message ?? "Remote request metrics are \(activity.status).", color: .orange)
            }
            if activity?.status == "notConfigured" || activity?.status == "configurationIncomplete" {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Whole-bucket request metrics are needed", systemImage: "chart.bar.doc.horizontal").font(.callout.weight(.semibold))
                    Text("In the S3 bucket's Metrics settings, configure request metrics for the entire bucket. These are paid AWS metrics and begin collecting after enablement; earlier uploads are not backfilled. Then choose Refresh bucket activity here.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Color(nsColor: .windowBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 14))
            }
            HStack(spacing: 12) {
                metricCard("Reported uploads", value: bytes(activity?.summary?.uploadedBytes), detail: "Last \(activityHours)h · all clients", symbol: "arrow.up.to.line")
                metricCard("Reported downloads", value: bytes(activity?.summary?.downloadedBytes), detail: "Last \(activityHours)h · all clients", symbol: "arrow.down.to.line")
                metricCard("Latest upload rate", value: speed(activity?.current?.uploadBytesPerSecond), detail: "Average over measured minute", symbol: "arrow.up.forward")
                metricCard("Latest download rate", value: speed(activity?.current?.downloadBytesPerSecond), detail: "Average over measured minute", symbol: "arrow.down.forward")
            }
            chartCard("Remote upload & download throughput", subtitle: "MiB per second · one-minute AWS observations from every client") {
                timeChart(points: bucketTrafficPoints, color: moss, unit: "MiB/s", area: false, showSeries: true)
                    .chartXScale(domain: bucketTimeDomain)
            }
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(activity?.sourceTimestamp.map { "Latest AWS observation: \(Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened))" } ?? "Waiting for an AWS observation.")
                    if let observed = activity?.sourceTimestamp { Text("Observation age: \(duration(max(0, Date().timeIntervalSince1970 - observed))). AWS publication is delayed.") }
                }
                Spacer()
                if let coverage = activity?.coverage {
                    VStack(alignment: .trailing, spacing: 4) {
                        Text("\(integer(coverage.observedMinutes)) / \(integer(coverage.expectedMinutes)) minutes reported")
                        Text("Upload observations: \(integer(coverage.uploadedMinutes)) · Download: \(integer(coverage.downloadedMinutes))")
                    }
                }
            }.font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 12) {
                metricCard("PUT requests", value: integer(activity?.summary?.putRequests), detail: "Reported in selected window", symbol: "arrow.up.doc")
                metricCard("GET requests", value: integer(activity?.summary?.getRequests), detail: "Reported in selected window", symbol: "arrow.down.doc")
                metricCard("Client errors · 4xx", value: integer(activity?.summary?.clientErrors), detail: "Reported in selected window", symbol: "exclamationmark.circle", accent: .orange)
                metricCard("Server errors · 5xx", value: integer(activity?.summary?.serverErrors), detail: "Reported in selected window", symbol: "server.rack", accent: .orange)
            }
            if !bucketLatencyPoints.isEmpty {
                chartCard("Remote request latency", subtitle: "Milliseconds · first byte and total request duration") {
                    timeChart(points: bucketLatencyPoints, color: metricsBlue, unit: "ms", area: false, showSeries: true)
                        .chartXScale(domain: bucketTimeDomain)
                }
            }
            Text("Reported bytes are the observed subtotal for this window. Missing minutes stay unknown. Upload traffic does not establish net storage growth, and it does not include every kind of AWS billable traffic.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let assumptions = activity?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Remote activity scope & coverage") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    }.padding(.top, 8)
                }.font(.callout)
            }
        }
    }

    private var bucketTimeDomain: ClosedRange<Date> {
        let end = activity?.windowEnd ?? Date().timeIntervalSince1970
        let start = activity?.windowStart ?? end - Double(activityHours * 3600)
        return Date(timeIntervalSince1970: min(start, end - 60))...Date(timeIntervalSince1970: end)
    }

    private func bucketPoints(_ keyPath: KeyPath<BucketActivityPoint, Double?>, category: String, scale: Double = 1) -> [MetricChartPoint] {
        var segment = 0
        var previousTime: Double?
        return (activity?.history ?? []).sorted { $0.timestamp < $1.timestamp }.compactMap { item in
            defer { previousTime = item.timestamp }
            if item[keyPath: keyPath] == nil || previousTime.map({ item.timestamp - $0 > 60 }) == true { segment += 1 }
            guard let value = item[keyPath: keyPath], value.isFinite else { return nil }
            return MetricChartPoint(id: "\(category)-\(item.timestamp)", date: Date(timeIntervalSince1970: item.timestamp), value: value / scale, series: "\(category)-\(segment)", category: category)
        }
    }
    private var bucketTrafficPoints: [MetricChartPoint] {
        bucketPoints(\.uploadBytesPerSecond, category: "Uploads", scale: 1_048_576)
            + bucketPoints(\.downloadBytesPerSecond, category: "Downloads", scale: 1_048_576)
    }
    private var bucketLatencyPoints: [MetricChartPoint] {
        bucketPoints(\.firstByteLatencyMs, category: "First byte")
            + bucketPoints(\.totalRequestLatencyMs, category: "Total request")
    }

    private var liveSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeading("Local transfer & cache", subtitle: "Current mount session · combined transfer traffic", symbol: "arrow.up.arrow.down")
            if let failure = snapshotFailure { message(failure, color: .orange) }
            if let note = snapshot?.message, !note.isEmpty { message(note, color: snapshot?.status == "available" && snapshotFailure == nil ? moss : .secondary) }
            if current?.cacheOutOfSpace == true { message("The local cache is out of space. Close unused files or increase the cache target before more transfers.", color: .orange) }
            HStack(spacing: 12) {
                metricCard("Transferred", value: bytes(current?.transferredBytes), detail: count(current?.transferCount, suffix: "completed transfers"), symbol: "arrow.up.arrow.down")
                metricCard("Current speed", value: speed(current?.speedBytesPerSecond), detail: "Average \(speed(current?.averageSpeedBytesPerSecond))", symbol: "speedometer")
                metricCard("Local cache", value: bytes(current?.cacheBytes), detail: count(current?.cacheFiles, suffix: "cached files"), symbol: "internaldrive")
                metricCard("Transfer errors", value: integer(current?.errors), detail: count(current?.activeTransfers, suffix: "active transfers"), symbol: "exclamationmark.circle", accent: (current?.errors ?? 0) > 0 ? .orange : moss)
            }
            HStack {
                Text("Recent activity").font(.headline).foregroundStyle(ink)
                Spacer()
                Picker("History range", selection: $historyMinutes) {
                    Text("15 min").tag(15); Text("1 hour").tag(60); Text("24 hours").tag(1440)
                }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("History range").frame(width: 240)
            }.padding(.top, 4)
            chartCard("Transfer throughput", subtitle: "MiB per second · upload and download totals are combined") {
                timeChart(points: activityPoints(\.speedBytesPerSecond, scale: 1_048_576), color: moss, unit: "MiB/s", area: true)
            }
            HStack(alignment: .top, spacing: 16) {
                chartCard("Cache footprint", subtitle: "MiB on this Mac · target \(bytes(current?.cacheLimitBytes))") {
                    timeChart(points: activityPoints(\.cacheBytes, scale: 1_048_576), color: metricsBlue, unit: "MiB", area: true)
                }
                chartCard("Upload queue", subtitle: "Waiting \(integer(current?.uploadsQueued)) · uploading \(integer(current?.uploadsInProgress))") {
                    timeChart(points: uploadPoints, color: metricsGold, unit: "files", area: false, showSeries: true)
                }
            }
            HStack(spacing: 24) {
                smallFact("Session duration", value: duration(current?.elapsedSeconds))
                smallFact("File checks", value: integer(current?.checks))
                smallFact("Cache errors", value: integer(current?.cacheErrors))
                Spacer()
                if let timestamp = snapshot?.timestamp { Text("Updated \(Date(timeIntervalSince1970: timestamp), style: .time)").font(.caption).foregroundStyle(.secondary) }
            }.padding(.horizontal, 4)
            Text("Transferred bytes describe this mount session, not your provider’s billable bandwidth. A reconnect starts a new session; gaps and unavailable readings are left empty.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var sftpStorageSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                sectionHeading("Remote server storage", subtitle: "Server filesystem · includes data outside the selected folder", symbol: "server.rack")
                Spacer()
                Button { storageRequestID = UUID() } label: { Label("Refresh storage", systemImage: "arrow.clockwise") }.disabled(storageLoading)
            }
            if storageLoading {
                HStack { ProgressView().controlSize(.small); Text("Asking the SFTP server for filesystem capacity…").font(.callout).foregroundStyle(.secondary) }
            }
            if let failure = storageFailure { message(failure, color: .orange) }
            if let storage, storage.status != "available" {
                message(storage.message ?? "This server has not provided complete filesystem capacity statistics.", color: .orange)
            }
            HStack(spacing: 12) {
                metricCard("Server capacity", value: bytes(storage?.totalBytes), detail: "Total reported filesystem space", symbol: "externaldrive")
                metricCard("Used on server", value: bytes(storage?.usedBytes), detail: "Across the remote filesystem", symbol: "chart.pie")
                metricCard("Free on server", value: bytes(storage?.freeBytes), detail: "Available space reported by server", symbol: "externaldrive.badge.plus")
            }
            HStack(alignment: .top, spacing: 16) {
                chartCard("Reported used & free", subtitle: "Remote filesystem space, not local cache") {
                    if let used = storage?.usedBytes, let free = storage?.freeBytes, used + free > 0 {
                        HStack(spacing: 20) {
                            Chart {
                                SectorMark(angle: .value("Used bytes", used), innerRadius: .ratio(0.7), angularInset: 1.5).foregroundStyle(moss)
                                    .accessibilityLabel("Used on server").accessibilityValue(bytes(used))
                                SectorMark(angle: .value("Free bytes", free), innerRadius: .ratio(0.7), angularInset: 1.5).foregroundStyle(moss.opacity(0.18))
                                    .accessibilityLabel("Free on server").accessibilityValue(bytes(free))
                            }.frame(width: 160, height: 170)
                            VStack(alignment: .leading, spacing: 12) {
                                Label("Used \(bytes(used))", systemImage: "circle.fill").foregroundStyle(moss)
                                Label("Free \(bytes(free))", systemImage: "circle").foregroundStyle(.secondary)
                                if let total = storage?.totalBytes, total > 0, used <= total {
                                    Text((used / total).formatted(.percent.precision(.fractionLength(1))) + " of reported capacity used")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }.font(.caption)
                        }
                    } else { emptyChart("The server has not reported usable used/free measurements.") }
                }
                chartCard("Observed server usage", subtitle: "GiB used · sampled on remote refresh") {
                    timeChart(points: sftpStoragePoints, color: metricsBlue, unit: "GiB", area: true)
                }
            }
            HStack(spacing: 12) {
                Image(systemName: "network").foregroundStyle(moss)
                Text(storage?.sourceTimestamp.map { "Measured \(Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened))" } ?? "No remote measurement available.")
                Spacer()
                Text("SFTP filesystem statistics")
            }.font(.caption).foregroundStyle(.secondary)
            Text("These values come from the server's filesystem statistics, which may cover other folders and users on the same volume. They are not the size of this drive's selected folder. Reserved filesystem space may not appear in the used/free breakdown. No recursive file scan is performed.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("SFTP does not report a provider price or bill. No storage cost is inferred for this server.")
                .font(.caption).foregroundStyle(.secondary)
            if let assumptions = storage?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Remote measurement details") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private var sftpStoragePoints: [MetricChartPoint] {
        var segment = 0
        var previousTime: Double?
        return storageHistory.compactMap { item in
            defer { previousTime = item.timestamp }
            if item.usedBytes == nil || previousTime.map({ item.timestamp - $0 > 3600 }) == true { segment += 1 }
            guard let value = item.usedBytes, value.isFinite else { return nil }
            return MetricChartPoint(id: "\(item.timestamp)", date: Date(timeIntervalSince1970: item.timestamp), value: value / 1_073_741_824, series: "Capacity-\(segment)")
        }
    }

    private var cloudSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                sectionHeading("Bucket storage · daily measurement", subtitle: "AWS CloudWatch · uploads appear in later daily measurements", symbol: "cloud")
                Spacer()
                Button { storageRequestID = UUID() } label: { Label("Refresh storage", systemImage: "arrow.clockwise") }.disabled(storageLoading)
            }
            if storageLoading { HStack { ProgressView().controlSize(.small); Text("Reading daily cloud storage metrics…").font(.callout).foregroundStyle(.secondary) } }
            if let failure = storageFailure { message(failure, color: .orange) }
            if let storage, storage.status != "available" || storage.isStale == true {
                message(storage.message ?? "Cloud storage data is \(storage.status). Missing measurements are not treated as zero.", color: .orange)
            }
            HStack(spacing: 12) {
                metricCard("Total storage", value: bytes(storage?.totalBytes), detail: storage?.totalBytes == nil ? "Awaiting a complete measurement" : "Reported bucket storage", symbol: "externaldrive.badge.icloud")
                metricCard("Stored objects", value: integer(storage?.objectCount), detail: "Provider-reported object count", symbol: "square.stack.3d.up")
                metricCard("Storage / month", value: currency(storage?.estimate?.monthlyUSD), detail: "AWS rates × daily measured storage", symbol: "dollarsign.circle")
            }
            HStack(spacing: 12) {
                Image(systemName: "clock").foregroundStyle(moss)
                Text(storage?.sourceTimestamp.map { "Measurement day: \(utcDayLabel($0)) UTC" } ?? "No complete cloud measurement available.")
                Spacer()
                if let queriedAt = storage?.queriedAt { Text("Checked \(Date(timeIntervalSince1970: queriedAt), style: .time)") }
            }.font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 16) {
                chartCard("30-day storage history", subtitle: "GiB · complete daily measurements (UTC)") {
                    timeChart(points: storagePoints(bytes: true), color: moss, unit: "GiB", area: true, utcDays: true)
                }
                chartCard("30-day object history", subtitle: "Objects · complete daily measurements (UTC)") {
                    timeChart(points: storagePoints(bytes: false), color: metricsBlue, unit: "objects", area: false, utcDays: true)
                }
            }
            chartCard("Storage classes", subtitle: "Reported bytes by storage class") {
                if classes.isEmpty { emptyChart(storage?.totalBytes == 0 ? "No storage in this complete measurement." : "Storage-class measurements are unavailable.") }
                else {
                    HStack(spacing: 28) {
                        Chart(classes) { item in
                            SectorMark(angle: .value("Bytes", item.bytes), innerRadius: .ratio(0.68), angularInset: 1.5)
                                .foregroundStyle(by: .value("Storage class", item.label))
                                .accessibilityLabel(item.label).accessibilityValue(bytes(item.bytes))
                        }.chartForegroundStyleScale(range: metricsColors).chartLegend(.hidden).frame(width: 200, height: 200)
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Array(classes.enumerated()), id: \.element.id) { index, item in
                                HStack {
                                    Circle().fill(metricsColors[index % metricsColors.count]).frame(width: 8, height: 8)
                                    Text(item.label).lineLimit(1)
                                    Spacer()
                                    Text(bytes(item.bytes)).monospacedDigit()
                                }.font(.callout)
                            }
                        }.frame(maxWidth: .infinity)
                    }
                }
            }
            Text("This is a daily AWS measurement, so ongoing uploads may not be included yet. Upload traffic is not net bucket growth: overwrites, deleted objects, multipart data, and versions can change the result. Storage totals are never extrapolated from transfer traffic.")
                .font(.caption).foregroundStyle(.secondary)
            if let assumptions = storage?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Storage measurement details") {
                    VStack(alignment: .leading, spacing: 6) { ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) } }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private var awsPricingSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                sectionHeading("AWS storage pricing", subtitle: "Actual regional rates from the AWS Price List API", symbol: "dollarsign.circle")
                Spacer()
                if let source = storage?.estimate?.sourceURL, let url = URL(string: source), url.scheme == "https" {
                    Link("AWS source", destination: url).font(.callout)
                }
            }
            HStack(spacing: 12) {
                metricCard("Storage / month", value: currency(storage?.estimate?.monthlyUSD), detail: "Estimate from daily measured storage", symbol: "calendar")
                metricCard("Price region", value: storage?.estimate?.region ?? connection.region, detail: "AWS public on-demand pricing", symbol: "globe")
                metricCard("Measured storage", value: bytes(storage?.totalBytes), detail: storage?.sourceTimestamp.map { utcDayLabel($0) + " UTC" } ?? "No complete measurement", symbol: "externaldrive")
            }
            if let estimate = storage?.estimate, estimate.status != "available" {
                message(estimate.message ?? "A complete automatic storage estimate is unavailable for these measurements.", color: .orange)
                if estimate.status == "partial", let known = estimate.knownMonthlyUSD {
                    Text("Priced classes only: \(currency(known))/month; the complete total is unknown.").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let components = storage?.estimate?.components, !components.isEmpty {
                VStack(spacing: 0) {
                    HStack {
                        Text("AWS storage product").frame(maxWidth: .infinity, alignment: .leading)
                        Text("Measured GiB").frame(width: 105, alignment: .trailing)
                        Text("USD / GiB-month").frame(width: 135, alignment: .trailing)
                        Text("USD / month").frame(width: 100, alignment: .trailing)
                    }.font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.bottom, 10)
                    ForEach(components) { item in
                        Divider()
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.label).font(.callout.weight(.medium))
                                if let sku = item.sku { Text("SKU \(sku)").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled) }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Text((item.bytes / 1_073_741_824).formatted(.number.precision(.fractionLength(0...3)))).frame(width: 105, alignment: .trailing)
                            Text(item.effectiveRateUSDPerGiBMonth.map { "$" + $0.formatted(.number.precision(.fractionLength(2...6))) } ?? "Unavailable").frame(width: 135, alignment: .trailing)
                            Text(currency(item.monthlyUSD)).frame(width: 100, alignment: .trailing)
                        }.font(.callout).monospacedDigit().padding(.vertical, 12)
                    }
                }.padding(18).background(Color(nsColor: .windowBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 14))
            }
            HStack(spacing: 18) {
                if let published = storage?.estimate?.pricingPublishedAt {
                    Text("AWS prices published \(Date(timeIntervalSince1970: published).formatted(date: .abbreviated, time: .shortened))")
                }
                if let fetched = storage?.estimate?.pricingFetchedAt {
                    Text("Fetched \(Date(timeIntervalSince1970: fetched).formatted(date: .abbreviated, time: .shortened))")
                }
            }.font(.caption).foregroundStyle(.secondary)
            Text("The rates come from AWS. The monthly amount is a storage-only estimate using the dated daily measurement above, not an AWS invoice or a live total of today's uploads. Effective rates reflect matched pricing tiers. Requests, transfer, retrieval and other charges are excluded.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let assumptions = storage?.estimate?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("AWS pricing method & exclusions") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private var actualSpendSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                sectionHeading("Actual AWS spend", subtitle: "Account-wide S3 · all buckets and all regions", symbol: "creditcard")
                Spacer()
                Button { billingRequestID = UUID() } label: { Label("Refresh spend", systemImage: "arrow.clockwise") }.disabled(billingLoading)
            }
            Text("This is the account's reported S3 spend from AWS Cost Explorer. It is not attributed to \(connection.bucket) alone.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if billingLoading { HStack { ProgressView().controlSize(.small); Text("Reading reported S3 spend…").font(.callout).foregroundStyle(.secondary) } }
            if let failure = billingFailure { message(failure, color: .orange) }
            if let billing, billing.status != "available" {
                message(billing.message ?? "AWS Cost Explorer spend is \(billing.status).", color: .orange)
            }
            HStack(spacing: 12) {
                metricCard("Reported month-to-date", value: billingMoney(billing?.total), detail: "Through completed UTC days", symbol: "calendar")
                metricCard("AWS account", value: billing?.accountID ?? "Unavailable", detail: "All S3 buckets · all regions", symbol: "person.crop.circle")
                metricCard("Billing data", value: billing == nil ? "—" : billing?.total == nil ? "Unavailable" : billing?.estimated == true ? "Provisional" : "Reported",
                           detail: billing?.estimated == true ? "AWS may revise these amounts" : "As returned by Cost Explorer", symbol: "doc.text")
            }
            if let start = billing?.monthStart, let end = billing?.periodEnd {
                Text("Billing period: \(start) through \(end) exclusive (UTC). Today's incomplete day is excluded.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            chartCard("Daily account S3 spend", subtitle: "\(billing?.currency ?? "Reported currency") · UTC dates · credits can be negative") {
                if let days = billing?.history, !days.isEmpty {
                    Chart(days) { day in
                        BarMark(x: .value("UTC day", Int(floor(day.timestamp / 86_400))), y: .value("Amount", day.amount))
                            .foregroundStyle(day.amount < 0 ? metricsBlue : day.estimated ? moss.opacity(0.55) : moss)
                            .accessibilityLabel("\(day.date)\(day.estimated ? ", provisional" : "")")
                            .accessibilityValue(billingMoney(day.amount))
                    }.chartXAxis {
                        AxisMarks(values: billingDayTicks(days)) { value in
                            AxisGridLine(); AxisTick()
                            AxisValueLabel {
                                if let day = value.as(Int.self) { Text(utcDayLabel(Double(day) * 86_400, includeYear: false)) }
                            }
                        }
                    }.chartXAxisLabel("UTC day")
                        .chartYAxis { AxisMarks(position: .leading) }.chartYAxisLabel(billing?.currency ?? "Amount").frame(height: 190)
                } else { emptyChart("No daily AWS spend measurements are available.") }
            }
            HStack(spacing: 16) {
                if let timestamp = billing?.queriedAt {
                    Text("Cost Explorer checked \(Date(timeIntervalSince1970: timestamp).formatted(date: .abbreviated, time: .shortened))")
                }
                if billing?.cached == true { Text("Cached response") }
            }.font(.caption).foregroundStyle(.secondary)
            Text("AWS billing can lag activity by 24 hours or more. Results are cached for six hours; Refresh spend respects that cache. Each uncached Cost Explorer API request costs $0.01. Provisional values can change as AWS completes billing.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let assumptions = billing?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Spend scope & billing details") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private func utcDayLabel(_ timestamp: Double, includeYear: Bool = true) -> String {
        let formatter = DateFormatter()
        formatter.locale = .current
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.setLocalizedDateFormatFromTemplate(includeYear ? "MMM d yyyy" : "MMM d")
        return formatter.string(from: Date(timeIntervalSince1970: timestamp))
    }

    private func billingDayTicks(_ days: [AWSBillingDay]) -> [Int] {
        let indices = days.map { Int(floor($0.timestamp / 86_400)) }
        guard let first = indices.min(), let last = indices.max() else { return [] }
        let step = max(1, Int(ceil(Double(last - first) / 4)))
        return Array(Set(Array(stride(from: first, through: last, by: step)) + [last])).sorted()
    }

    private func utcDateTicks(_ points: [MetricChartPoint]) -> [Date] {
        let indices = points.map { Int(floor($0.date.timeIntervalSince1970 / 86_400)) }
        guard let first = indices.min(), let last = indices.max() else { return [] }
        let step = max(1, Int(ceil(Double(last - first) / 3)))
        return Array(Set(Array(stride(from: first, through: last, by: step)) + [last])).sorted()
            .map { Date(timeIntervalSince1970: Double($0) * 86_400) }
    }

    private func billingMoney(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        guard let currency = billing?.currency, !currency.isEmpty else { return value.formatted() + " (currency unavailable)" }
        return value.formatted(.currency(code: currency))
    }

    private var costSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeading("Optional storage scenario", subtitle: "Adjust assumptions to explore a scenario", symbol: "chart.bar.xaxis")
            HStack(spacing: 12) {
                Label(estimateMethod, systemImage: manualRateEntered ? "slider.horizontal.3" : "checkmark.seal").font(.callout.weight(.medium)).foregroundStyle(moss)
                Spacer()
                if !manualRateEntered, let source = storage?.estimate?.sourceURL, let url = URL(string: source), url.scheme == "https" {
                    Link("Pricing source", destination: url).font(.callout)
                }
            }
            if !manualRateEntered, let fetched = storage?.estimate?.pricingFetchedAt {
                Text("Public prices checked \(Date(timeIntervalSince1970: fetched).formatted(date: .abbreviated, time: .shortened)).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if storage?.isStale == true || storage?.status == "partial" {
                message("These estimates use the latest complete storage measurement shown above. Newer storage changes may be missing.", color: .orange)
            }
            if !manualRateEntered, storage?.estimate?.status == "unavailable", storage?.totalBytes != nil, !storageLoading {
                message("Automatic pricing is unavailable. Enter your blended rate, or try Refresh storage again.", color: .orange)
            }
            if !manualRateEntered, storage?.estimate?.status == "partial" {
                message("Only some storage classes have public pricing available. The total estimate is unavailable until all classes can be priced, or you enter a blended rate.", color: .orange)
                if let known = storage?.estimate?.knownMonthlyUSD {
                    Text("Priced classes only: \(currency(known)) per month. This is a partial estimate.").font(.caption).foregroundStyle(.secondary)
                }
                if let unsupported = storage?.estimate?.unsupportedStorageTypes, !unsupported.isEmpty {
                    Text("Not yet priced: \(unsupported.joined(separator: ", ")).").font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack(alignment: .top, spacing: 24) {
                VStack(alignment: .leading, spacing: 9) {
                    Text("Blended rate override (optional)").font(.callout.weight(.medium))
                    HStack {
                        Text("$").foregroundStyle(.secondary)
                        TextField("Automatic", text: $rateText).textFieldStyle(.roundedBorder).frame(width: 120)
                            .accessibilityLabel("Blended storage rate in US dollars per GiB per month")
                        Text("/ GiB / month").font(.callout).foregroundStyle(.secondary)
                    }
                    if manualRateEntered && rate == nil { Text("Enter a rate from 0 to 1,000,000 using a decimal point.").font(.caption).foregroundStyle(.red) }
                    Text("Leave blank for public AWS storage pricing. Enter your own rate for your region, classes and agreement. Your override is saved for this drive.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }.frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 9) {
                    HStack {
                        Text("Assumed monthly storage change").font(.callout.weight(.medium))
                        Spacer()
                        Text(String(format: "%+.0f%%", monthlyGrowth)).font(.callout.monospacedDigit()).foregroundStyle(moss)
                    }
                    Slider(value: $monthlyGrowth, in: -25...100, step: 5).accessibilityLabel("Assumed monthly storage growth percent")
                    Text(monthlyGrowth == 0 ? "Storage stays constant in this scenario." : "Storage compounds by this percentage each month.")
                        .font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(18).background(Color(nsColor: .windowBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 14))
            HStack(spacing: 12) {
                metricCard("Month 1 estimate", value: currency(monthlyCost), detail: estimateDetail, symbol: "calendar")
                metricCard("Month 12", value: currency(scenario.last?.monthlyUSD), detail: "With your storage assumption", symbol: "calendar.badge.clock")
                metricCard("12-month total", value: scenario.isEmpty ? "—" : currency(scenario.reduce(0) { $0 + $1.monthlyUSD }), detail: "Sum of this scenario", symbol: "sum")
            }
            chartCard("12-month storage scenario", subtitle: "Estimated monthly storage cost in USD · not an invoice or forecast") {
                if scenario.isEmpty { emptyChart(scenarioUnavailable) }
                else {
                    Chart(scenario) { point in
                        AreaMark(x: .value("Month", point.month), y: .value("USD", point.monthlyUSD))
                            .foregroundStyle(LinearGradient(colors: [moss.opacity(0.24), moss.opacity(0.02)], startPoint: .top, endPoint: .bottom))
                        LineMark(x: .value("Month", point.month), y: .value("USD", point.monthlyUSD)).foregroundStyle(moss).lineStyle(StrokeStyle(lineWidth: 2.5))
                        PointMark(x: .value("Month", point.month), y: .value("USD", point.monthlyUSD)).foregroundStyle(moss).symbolSize(22)
                            .accessibilityLabel("Month \(point.month)").accessibilityValue(currency(point.monthlyUSD))
                    }.chartXAxis { AxisMarks(values: [1, 3, 6, 9, 12]) }.chartXAxisLabel("Month")
                        .chartYAxis { AxisMarks(position: .leading) }.chartYAxisLabel("USD / month").frame(height: 190)
                }
            }
            Text(manualRateEntered
                 ? "Month 1 = reported GiB × your blended USD/GiB/month rate. Later months apply your chosen storage change at that rate."
                 : "Month 1 uses public AWS storage prices and reported storage classes. Later months scale that baseline with your storage assumption, keeping the effective rate and class mix constant; future tier changes are not recalculated.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("Storage only, not billed spend. Requests, data transfer, retrieval, minimum-duration charges, replication, negotiated discounts and taxes are excluded. Changing assumptions does not request new cloud data.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if !manualRateEntered, let assumptions = storage?.estimate?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Public pricing method & assumptions") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private func activityPoints(_ keyPath: KeyPath<DriveMetricPoint, Double?>, scale: Double) -> [MetricChartPoint] {
        var segment = 0
        var previous: DriveMetricPoint?
        return currentHistory.compactMap { item in
            defer { previous = item }
            if item[keyPath: keyPath] == nil || (previous.map { item.timestamp - $0.timestamp > 90 || item.sessionID != $0.sessionID } ?? false) { segment += 1 }
            guard let value = item[keyPath: keyPath], value.isFinite else { return nil }
            return MetricChartPoint(id: "\(item.timestamp)", date: Date(timeIntervalSince1970: item.timestamp), value: value / scale, series: "segment-\(segment)")
        }
    }
    private var uploadPoints: [MetricChartPoint] {
        let waiting = activityPoints(\.uploadsQueued, scale: 1).map { MetricChartPoint(id: "waiting-\($0.id)", date: $0.date, value: $0.value, series: "waiting-\($0.series)", category: "Waiting") }
        let uploading = activityPoints(\.uploadsInProgress, scale: 1).map { MetricChartPoint(id: "uploading-\($0.id)", date: $0.date, value: $0.value, series: "uploading-\($0.series)", category: "Uploading") }
        return waiting + uploading
    }

    private func storagePoints(bytes: Bool) -> [MetricChartPoint] {
        var segment = 0
        var previousTime: Double?
        return storageHistory.compactMap { item in
            defer { previousTime = item.timestamp }
            let value = bytes ? item.totalBytes.map { $0 / 1_073_741_824 } : item.objectCount
            if item.complete != true || value == nil || previousTime.map({ item.timestamp - $0 > 86_400 }) == true { segment += 1 }
            guard item.complete == true, let value, value.isFinite else { return nil }
            return MetricChartPoint(id: "\(item.timestamp)", date: Date(timeIntervalSince1970: item.timestamp), value: value, series: "Storage-\(segment)")
        }
    }

    @ViewBuilder private func timeChart(points: [MetricChartPoint], color: Color, unit: String, area: Bool, showSeries: Bool = false, utcDays: Bool = false) -> some View {
        let isolatedSeries = Set(Dictionary(grouping: points, by: \.series).filter { $0.value.count == 1 }.keys)
        if points.isEmpty { emptyChart("No measurements in this period yet.") }
        else {
            Chart(points) { point in
                if area {
                    AreaMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(LinearGradient(colors: [color.opacity(0.22), color.opacity(0.02)], startPoint: .top, endPoint: .bottom))
                }
                if showSeries {
                    LineMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(by: .value("Series", point.category ?? point.series)).interpolationMethod(.stepEnd)
                } else {
                    LineMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(color).lineStyle(StrokeStyle(lineWidth: 2))
                }
                if points.count <= 35 || isolatedSeries.contains(point.series) {
                    if showSeries {
                        PointMark(x: .value("Time", point.date), y: .value(unit, point.value))
                            .foregroundStyle(by: .value("Series", point.category ?? point.series)).symbolSize(16)
                            .accessibilityLabel("\(point.category ?? "Queue"), \(point.date.formatted())").accessibilityValue("\(point.value.formatted()) \(unit)")
                    } else {
                        PointMark(x: .value("Time", point.date), y: .value(unit, point.value)).foregroundStyle(color).symbolSize(16)
                            .accessibilityLabel(utcDays ? utcDayLabel(point.date.timeIntervalSince1970) + " UTC" : point.date.formatted()).accessibilityValue("\(point.value.formatted()) \(unit)")
                    }
                }
            }.chartForegroundStyleScale(range: [metricsGold, metricsBlue])
                .chartLegend(showSeries ? .visible : .hidden)
                .chartYAxis { AxisMarks(position: .leading) }.chartYAxisLabel(unit)
                .chartXAxis {
                    if utcDays {
                        AxisMarks(values: utcDateTicks(points)) { value in
                            AxisGridLine(); AxisTick()
                            AxisValueLabel {
                                if let date = value.as(Date.self) { Text(utcDayLabel(date.timeIntervalSince1970, includeYear: false)) }
                            }
                        }
                    } else { AxisMarks(values: .automatic(desiredCount: 4)) }
                }
                .frame(height: 170)
        }
    }

    private func sectionHeading(_ title: String, subtitle: String, symbol: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).font(.title3).foregroundStyle(moss)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.title3.weight(.semibold)).foregroundStyle(ink)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private func metricCard(_ title: String, value: String, detail: String, symbol: String, accent: Color = moss) -> some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack { Text(title).font(.caption.weight(.medium)).foregroundStyle(.secondary); Spacer(); Image(systemName: symbol).foregroundStyle(accent) }
            Text(value).font(.system(size: 25, weight: .semibold, design: .rounded)).foregroundStyle(ink).monospacedDigit().lineLimit(1).minimumScaleFactor(0.65)
            Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2).frame(height: 26, alignment: .top)
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Color(nsColor: .windowBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 14)).overlay(RoundedRectangle(cornerRadius: 14).stroke(moss.opacity(0.08)))
    }
    private func chartCard<Content: View>(_ title: String, subtitle: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.callout.weight(.semibold)).foregroundStyle(ink)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            content()
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).background(Color(nsColor: .windowBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 16)).overlay(RoundedRectangle(cornerRadius: 16).stroke(moss.opacity(0.08)))
    }
    private func emptyChart(_ text: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "chart.xyaxis.line").font(.title2).foregroundStyle(moss.opacity(0.4))
            Text(text).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }.frame(maxWidth: .infinity).frame(height: 170)
    }
    private func statusPill(_ text: String, color: Color) -> some View {
        HStack(spacing: 6) { Circle().fill(color).frame(width: 6, height: 6); Text(text).font(.caption.weight(.medium)) }
            .foregroundStyle(color).padding(.horizontal, 10).padding(.vertical, 7).background(color.opacity(0.08)).clipShape(Capsule())
    }
    private func message(_ text: String, color: Color) -> some View {
        Label(text, systemImage: "info.circle").font(.caption).foregroundStyle(color).fixedSize(horizontal: false, vertical: true)
    }
    private func smallFact(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) { Text(title).font(.caption).foregroundStyle(.secondary); Text(value).font(.callout.weight(.medium)).monospacedDigit() }
    }
    private func bytes(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0, value < Double(Int64.max) else { return "—" }
        if value == 0 { return "0 B" }
        return ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .binary)
    }
    private func speed(_ value: Double?) -> String { value.map { "\(bytes($0))/s" } ?? "—" }
    private func integer(_ value: Double?) -> String { value?.formatted(.number.precision(.fractionLength(0...2))) ?? "—" }
    private func count(_ value: Double?, suffix: String) -> String { value.map { "\(integer($0)) \(suffix)" } ?? "Not reported" }
    private func currency(_ value: Double?) -> String { value.map { $0.formatted(.currency(code: "USD")) } ?? "—" }
    private func duration(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0 else { return "—" }
        let formatter = DateComponentsFormatter(); formatter.allowedUnits = [.day, .hour, .minute, .second]; formatter.unitsStyle = .abbreviated; formatter.maximumUnitCount = 2
        return formatter.string(from: value) ?? "—"
    }
    private func refreshSnapshot() async {
        do {
            let result = try await MetricsClient.run(["snapshot", connection.id], as: DriveMetricSnapshot.self)
            try Task.checkCancellation(); snapshot = result; snapshotFailure = nil
        } catch { if !Task.isCancelled { snapshotFailure = error.localizedDescription } }
    }
    private func refreshBilling() async {
        billingLoading = true; billingFailure = nil
        do {
            let result = try await MetricsClient.run(["costs", connection.id], script: "cloud_billing.py", as: AWSBillingSnapshot.self)
            try Task.checkCancellation(); billing = result; billingLoading = false
        } catch { if !Task.isCancelled { billingFailure = error.localizedDescription; billingLoading = false } }
    }
    private func refreshActivity() async {
        activityLoading = true; activityFailure = nil
        do {
            let result = try await MetricsClient.run(["snapshot", connection.id, "--hours", String(activityHours)], script: "cloud_activity.py", as: BucketActivitySnapshot.self)
            try Task.checkCancellation(); activity = result; activityLoading = false
        } catch { if !Task.isCancelled { activityFailure = error.localizedDescription; activityLoading = false } }
    }
    private func refreshStorage() async {
        storageLoading = true; storageFailure = nil
        do {
            let result = try await MetricsClient.run(["storage", connection.id], as: DriveStorageSnapshot.self)
            try Task.checkCancellation(); storage = result; storageLoading = false
        } catch { if !Task.isCancelled { storageFailure = error.localizedDescription; storageLoading = false } }
    }
}
