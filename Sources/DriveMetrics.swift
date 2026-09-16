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
    static func run<T: Decodable>(_ arguments: [String], as type: T.Type) async throws -> T {
        let handle = MetricsProcess()
        let data: Data = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        guard let python = ServiceClient.pythonPath else { throw TurtleError(message: "Python is unavailable.") }
                        let process = Process(), output = Pipe()
                        process.executableURL = URL(fileURLWithPath: python)
                        process.arguments = [ServiceClient.resources.appendingPathComponent("service/drive_metrics.py").path,
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
                    liveSection
                    if connection.isSFTP { sftpStorageSection } else { cloudSection; costSection }
                    Text("Local values refresh every 5 seconds while this dashboard is open. History is stored locally. Remote storage refreshes once on opening and when you choose Refresh storage.")
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
            if snapshot == nil && snapshotFailure == nil { ProgressView().controlSize(.small) }
            statusPill(snapshotFailure != nil ? "Last reading" : snapshot?.status == "available" ? "Live" : snapshot?.status == "partial" ? "Partial data" : snapshot?.status == "disconnected" ? "Disconnected" : "Waiting for data",
                       color: snapshot?.status == "available" && snapshotFailure == nil ? moss : .secondary)
            Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
        }.padding(22)
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
                sectionHeading("Remote cloud storage", subtitle: connection.bucket, symbol: "cloud")
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
                metricCard("Storage / month", value: currency(monthlyCost), detail: estimateDetail, symbol: "dollarsign.circle")
            }
            HStack(spacing: 12) {
                Image(systemName: "clock").foregroundStyle(moss)
                Text(storage?.sourceTimestamp.map { "Measurement: \(Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened))" } ?? "No complete cloud measurement available.")
                Spacer()
                if let queriedAt = storage?.queriedAt { Text("Checked \(Date(timeIntervalSince1970: queriedAt), style: .time)") }
            }.font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 16) {
                chartCard("30-day storage history", subtitle: "GiB · complete daily measurements") {
                    timeChart(points: storagePoints(bytes: true), color: moss, unit: "GiB", area: true)
                }
                chartCard("30-day object history", subtitle: "Objects · complete daily measurements") {
                    timeChart(points: storagePoints(bytes: false), color: metricsBlue, unit: "objects", area: false)
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
            Text("Daily cloud metrics can lag behind changes. Storage and object counts may include versions and other provider-reported data. The drive can stay ejected while these metrics load.")
                .font(.caption).foregroundStyle(.secondary)
            if let assumptions = storage?.assumptions, !assumptions.isEmpty {
                DisclosureGroup("Storage measurement details") {
                    VStack(alignment: .leading, spacing: 6) { ForEach(assumptions, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) } }.padding(.top, 8)
                }.font(.callout)
            }
        }.padding(.top, 8)
    }

    private var costSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeading("Storage cost planner", subtitle: "Public AWS rates or your own · USD estimates", symbol: "chart.bar.xaxis")
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

    @ViewBuilder private func timeChart(points: [MetricChartPoint], color: Color, unit: String, area: Bool, showSeries: Bool = false) -> some View {
        if points.isEmpty { emptyChart("No measurements in this period yet.") }
        else {
            Chart(points) { point in
                if area {
                    AreaMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(LinearGradient(colors: [color.opacity(0.22), color.opacity(0.02)], startPoint: .top, endPoint: .bottom))
                }
                if showSeries {
                    LineMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(by: .value("Queue", point.category ?? point.series)).interpolationMethod(.stepEnd)
                } else {
                    LineMark(x: .value("Time", point.date), y: .value(unit, point.value), series: .value("Segment", point.series))
                        .foregroundStyle(color).lineStyle(StrokeStyle(lineWidth: 2))
                }
                if points.count <= 35 {
                    if showSeries {
                        PointMark(x: .value("Time", point.date), y: .value(unit, point.value))
                            .foregroundStyle(by: .value("Queue", point.category ?? point.series)).symbolSize(16)
                            .accessibilityLabel("\(point.category ?? "Queue"), \(point.date.formatted())").accessibilityValue("\(point.value.formatted()) \(unit)")
                    } else {
                        PointMark(x: .value("Time", point.date), y: .value(unit, point.value)).foregroundStyle(color).symbolSize(16)
                            .accessibilityLabel(point.date.formatted()).accessibilityValue("\(point.value.formatted()) \(unit)")
                    }
                }
            }.chartForegroundStyleScale(range: [metricsGold, metricsBlue])
                .chartLegend(showSeries ? .visible : .hidden)
                .chartYAxis { AxisMarks(position: .leading) }.chartYAxisLabel(unit)
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
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
    private func refreshStorage() async {
        storageLoading = true; storageFailure = nil
        do {
            let result = try await MetricsClient.run(["storage", connection.id], as: DriveStorageSnapshot.self)
            try Task.checkCancellation(); storage = result; storageLoading = false
        } catch { if !Task.isCancelled { storageFailure = error.localizedDescription; storageLoading = false } }
    }
}
