import SwiftUI

struct S3CostSummary: Decodable {
    var status: String
    var scope: String
    var message: String?
    var monthStart: String
    var periodEnd: String
    var total: Double?
    var currency: String?
    var estimated: Bool
    var projectedTotal: Double?
    var projectionMethod: String?
    var projectedPeriodEnd: String
    var observedDays: Int
    var daysInMonth: Int

    private var hasCompleteReport: Bool {
        status == "available" && scope == "accountS3AllRegions" && currency != nil
    }

    var actualValue: String {
        guard hasCompleteReport, let total, total.isFinite, let currency else { return unavailableValue }
        return total.formatted(.currency(code: currency))
    }

    var projectedValue: String {
        guard hasCompleteReport, projectionMethod == "completedDaysRunRate",
              let projectedTotal, projectedTotal.isFinite, let currency else { return unavailableValue }
        return "≈" + projectedTotal.formatted(.currency(code: currency))
    }

    private var unavailableValue: String {
        switch status {
        case "authenticationRequired": return "Sign in to view"
        case "permissionDenied": return "Billing access needed"
        case "partial": return "Incomplete data"
        case "noData": return "Not yet reported"
        default: return "Unavailable"
        }
    }

    private static var utcCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }

    private static func date(_ value: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = utcCalendar
        formatter.timeZone = utcCalendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        return formatter.date(from: value)
    }

    private static func formatted(_ date: Date, pattern: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = .current
        formatter.calendar = utcCalendar
        formatter.timeZone = utcCalendar.timeZone
        formatter.setLocalizedDateFormatFromTemplate(pattern)
        return formatter.string(from: date)
    }

    var actualPeriod: String {
        guard let start = Self.date(monthStart), let end = Self.date(periodEnd), end > start,
              let last = Self.utcCalendar.date(byAdding: .day, value: -1, to: end) else { return "Month to date" }
        return Self.formatted(start, pattern: "MMMd") + " – " + Self.formatted(last, pattern: "MMMd") + " · UTC"
    }

    var projectedPeriod: String {
        guard let start = Self.date(monthStart) else { return "Month-end estimate" }
        return Self.formatted(start, pattern: "MMMMyyyy") + " · average pace"
    }

    var actualHelp: String {
        (message ?? "Reported S3 spending for the AWS account, across all buckets and regions.")
            + " Includes completed UTC days only; AWS billing may lag activity."
            + (estimated ? " AWS marks these costs as provisional." : "")
    }

    var projectionHelp: String {
        guard projectedTotal != nil, projectionMethod == "completedDaysRunRate" else { return actualHelp }
        return "Estimated total for the full month, including spending already reported: the account’s S3 cost over \(observedDays) completed UTC days, divided by \(observedDays), multiplied by \(daysInMonth) days. Assumes the same daily spending pace, including any credits; this is an estimate, not an AWS forecast."
    }
}

struct S3CostSummaryRows: View {
    let connection: Connection
    @State private var summary: S3CostSummary?
    @State private var loading = true
    @State private var failure: String?
    @State private var requestID = UUID()

    var body: some View {
        VStack(spacing: 0) {
            costRow("Actual cost", value: summary?.actualValue ?? (loading ? "Checking…" : "Unavailable"),
                    period: summary?.actualPeriod ?? "Month to date", symbol: "creditcard")
                .help(failure ?? summary?.actualHelp ?? "Reading the account’s S3 spending.")
            Divider().padding(.leading, 42)
            costRow("Projected", value: summary?.projectedValue ?? (loading ? "Checking…" : "Unavailable"),
                    period: summary?.projectedPeriod ?? "Month-end estimate", symbol: "chart.line.uptrend.xyaxis")
                .help(failure ?? summary?.projectionHelp ?? "Estimated full-month total at the reported daily pace.")
            HStack(alignment: .top, spacing: 8) {
                Text("Account S3 · all buckets and regions")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button { requestID = UUID() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).foregroundStyle(moss)
                    .accessibilityLabel("Refresh S3 costs")
                    .help("Refresh reported costs. Reuses the six-hour billing cache.")
                    .disabled(loading)
            }.padding(.leading, 29).padding(.bottom, 13)
            if let failure {
                Text(failure).font(.system(size: 10)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 29).padding(.bottom, 13)
            }
        }.task(id: requestID) { await load() }
    }

    private func costRow(_ label: String, value: String, period: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 17).foregroundStyle(moss.opacity(0.8))
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            VStack(alignment: .trailing, spacing: 4) {
                Text(value).monospacedDigit().textSelection(.enabled)
                Text(period).font(.system(size: 10)).foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, alignment: .trailing)
                .fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 12)).padding(.vertical, 13)
    }

    @MainActor private func load() async {
        loading = true; failure = nil
        defer { if !Task.isCancelled { loading = false } }
        do {
            let data = try await ServiceClient.run(["costs", connection.id], scriptName: "cloud_billing.py")
            try Task.checkCancellation()
            summary = try JSONDecoder().decode(S3CostSummary.self, from: data)
        } catch is CancellationError { return }
        catch {
            guard !Task.isCancelled else { return }
            summary = nil
            failure = error.localizedDescription
        }
    }
}
