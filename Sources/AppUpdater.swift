import AppKit
import Combine
import Sparkle
import SwiftUI

/// Sparkle owns download verification, installation, error UI, and relaunch.
/// The app only coordinates its drive supervisor with macOS termination.
@MainActor final class AppUpdater: NSObject, ObservableObject, SPUUpdaterDelegate, NSMenuItemValidation {
    static let shared = AppUpdater()
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var automaticallyChecksForUpdates = true
    @Published private(set) var availableVersion: String?
    private(set) var installationPending = false
    private var preparingToQuit = false
    private var started = false
    private lazy var controller = SPUStandardUpdaterController(
        startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil)

    func start() {
        guard !started else { return }
        started = true
        controller.updater.publisher(for: \.canCheckForUpdates)
            .assign(to: &$canCheckForUpdates)
        controller.updater.publisher(for: \.automaticallyChecksForUpdates)
            .assign(to: &$automaticallyChecksForUpdates)
        Task {
            // Also recovers a cancelled or interrupted installation. With no
            // handoff marker this command makes no connection changes.
            if ServiceClient.pythonPath != nil {
                do { try await runServiceCommand("resume-update") }
                catch { AppModel.shared.error = error.localizedDescription }
            }
            controller.startUpdater()
        }
    }

    @objc func checkForUpdates(_ sender: Any? = nil) {
        guard canCheckForUpdates else { return }
        controller.checkForUpdates(sender)
    }

    func setAutomaticChecks(_ enabled: Bool) {
        controller.updater.automaticallyChecksForUpdates = enabled
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool { canCheckForUpdates }

    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        availableVersion = item.displayVersionString
    }

    func updaterDidNotFindUpdate(_ updater: SPUUpdater) {
        availableVersion = nil
    }

    func updater(_ updater: SPUUpdater, willInstallUpdate item: SUAppcastItem) {
        installationPending = true
    }

    func updater(_ updater: SPUUpdater, willExtractUpdate item: SUAppcastItem) {
        // Sparkle can install a staged update on an ordinary Quit, including
        // while its Ready to Install window is open. Arm the quit gate before
        // the external installer starts, not only on Install and Relaunch.
        installationPending = true
    }

    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem,
                 immediateInstallationBlock: @escaping () -> Void) -> Bool {
        installationPending = true
        return false
    }

    func terminationReply(for application: NSApplication) -> NSApplication.TerminateReply {
        guard installationPending else { return .terminateNow }
        guard !preparingToQuit else { return .terminateLater }
        guard AppModel.shared.activeAction == nil,
              AppModel.shared.transferRequest == nil, !AppModel.shared.otherSheetPresented else {
            AppModel.shared.error = "Finish the open connection action, then choose Install and Relaunch again."
            return .terminateCancel
        }
        preparingToQuit = true
        AppModel.shared.activeAction = "prepare-update"
        AppModel.shared.driveMessage = "Preparing drives for the update. Busy drives will stay connected."
        Task {
            do {
                try await runServiceCommand("prepare-update")
                application.reply(toApplicationShouldTerminate: true)
            } catch {
                // The service restores the original connection intent itself
                // before returning a preparation failure.
                preparingToQuit = false
                AppModel.shared.activeAction = nil
                AppModel.shared.driveMessage = nil
                application.reply(toApplicationShouldTerminate: false)
                AppModel.shared.error = error.localizedDescription
                await AppModel.shared.refresh()
            }
        }
        return .terminateLater
    }

    private func runServiceCommand(_ command: String) async throws {
        let data = try await ServiceClient.run([command])
        let response = try JSONDecoder().decode(ActionResponse.self, from: data)
        guard response.ok else {
            throw TurtleError(message: response.error ?? "The drives could not be prepared for the update.")
        }
    }
}

struct CheckForUpdatesButton: View {
    @ObservedObject private var updater = AppUpdater.shared
    var body: some View {
        Button("Check for Updates…") { updater.checkForUpdates() }
            .disabled(!updater.canCheckForUpdates)
    }
}

struct UpdateSettingsView: View {
    @ObservedObject private var updater = AppUpdater.shared
    private var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text("Version \(version)").foregroundStyle(.secondary)
                Spacer()
            }
            Button { updater.checkForUpdates() } label: {
                Label(updater.availableVersion.map { "Update to \($0)…" } ?? "Check for updates…",
                      systemImage: "arrow.down.circle")
            }.buttonStyle(.link).disabled(!updater.canCheckForUpdates)
            Toggle("Check automatically", isOn: Binding(
                get: { updater.automaticallyChecksForUpdates },
                set: { updater.setAutomaticChecks($0) }))
                .toggleStyle(.checkbox)
                .help("Check GitHub daily. You choose when to install and relaunch.")
        }.font(.system(size: 11))
    }
}
