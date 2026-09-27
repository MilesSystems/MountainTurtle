import SwiftUI
import AppKit
import UniformTypeIdentifiers

private struct CapturedFailure: Decodable, Identifiable {
    let id: String
    let operation: String
    let path: String
    let timestamp: Double
    let explanation: String
}

private struct FailureSnapshot: Decodable {
    let events: [CapturedFailure]
    let coverage: String
    let report: String
}

struct FailureReportView: View {
    let connection: Connection
    @Environment(\.dismiss) private var dismiss
    @State private var snapshot: FailureSnapshot?
    @State private var loading = false
    @State private var tab = "history"
    @State private var notes = ""
    @State private var message: String?

    private var report: String {
        "Mountain Turtle \(appVersion)\n\n" + (snapshot?.report ?? "")
            + "\n\nWhat happened / steps to reproduce:\n" + (notes.isEmpty ? "[Please describe the problem]" : notes)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Failures & reports", systemImage: "exclamationmark.bubble").font(.title2.weight(.semibold))
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text(connection.name).foregroundStyle(.secondary)
            Picker("View", selection: $tab) {
                Text("Failure history").tag("history")
                Text("Report preview").tag("report")
            }.pickerStyle(.segmented)
            if loading && snapshot == nil {
                ProgressView("Reading local failure history…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if tab == "history" {
                Text(snapshot?.coverage ?? "Failure history could not be loaded. Try refreshing.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if snapshot?.events.isEmpty == true {
                            Label("No recent backend failures captured", systemImage: "tray")
                                .font(.headline).padding(.top, 16)
                            Text("If Finder showed an error, choose Send to developer and describe it. A Finder-only rejection may not appear in the drive log.")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(snapshot?.events ?? []) { event in
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Label(event.operation.capitalized + " error", systemImage: "exclamationmark.triangle")
                                        .font(.headline).foregroundStyle(.orange)
                                    Spacer()
                                    Text(Date(timeIntervalSince1970: event.timestamp), format: .dateTime.month().day().hour().minute().second())
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                if !event.path.isEmpty {
                                    Text(event.path).font(.callout.monospaced()).textSelection(.enabled)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Text(event.explanation).font(.callout).textSelection(.enabled)
                                Divider()
                            }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                }.background(cream).clipShape(RoundedRectangle(cornerRadius: 10))
            } else {
                Text("Review exactly what you will share. Generated diagnostics exclude filenames, server details, credentials and raw logs. Check your own description for private information.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Text("What happened?").font(.headline)
                TextEditor(text: $notes).font(.callout).frame(height: 72)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.secondary.opacity(0.3)))
                    .accessibilityLabel("Steps to reproduce or Finder error message")
                    .onChange(of: notes) { _, value in if value.count > 2000 { notes = String(value.prefix(2000)) } }
                ScrollView {
                    Text(report).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                }.background(cream).clipShape(RoundedRectangle(cornerRadius: 10))
                Text("GitHub issues are public. The button copies this report and opens a draft; paste it there and submit only when ready. Nothing is sent automatically.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let message { Text(message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
            HStack {
                Button("Refresh history") { Task { await refresh() } }.disabled(loading)
                Spacer()
                if tab == "history" {
                    Button("Send to developer…") { tab = "report" }.buttonStyle(.borderedProminent)
                        .disabled(snapshot == nil)
                } else {
                    Button("Save report…") { save() }.disabled(snapshot == nil)
                    Button("Copy report & open GitHub") { share() }.buttonStyle(.borderedProminent)
                        .disabled(snapshot == nil)
                }
            }
        }.padding(24).frame(width: 760, height: 670)
        .task { await refresh() }
    }

    @MainActor private func refresh() async {
        loading = true
        defer { loading = false }
        do {
            let data = try await ServiceClient.run(["failure-report", connection.id])
            snapshot = try JSONDecoder().decode(FailureSnapshot.self, from: data)
            message = nil
        } catch {
            message = "Could not read local failure history. Refresh to try again; no report was sent."
        }
    }

    @MainActor private func save() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = "Mountain-Turtle-report.txt"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try report.write(to: url, atomically: true, encoding: .utf8)
            message = "Report saved. Nothing was sent."
        } catch { message = "Could not save the report. Choose another location and try again." }
    }

    @MainActor private func share() {
        NSPasteboard.general.clearContents()
        guard NSPasteboard.general.setString(report, forType: .string) else {
            message = "Could not copy the report. Try Save report instead."
            return
        }
        let url = URL(string: "https://github.com/MilesSystems/MountainTurtle/issues/new")!
        if NSWorkspace.shared.open(url) {
            message = "Report copied. Paste it into the GitHub issue and review before submitting."
        } else {
            message = "Report copied, but GitHub could not be opened. Save the report or open the repository’s Issues page manually."
        }
    }
}
