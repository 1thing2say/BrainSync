import Foundation
import UserNotifications

/// UserNotifications for control-state changes, coalesced (the last change within 1.5 s wins).
/// Only inside an app bundle: UNUserNotificationCenter needs a bundle id, so `swift run` skips it.
@MainActor
final class Notifier {
    private let enabled: Bool
    private var pending: (String, String)?
    private var timer: Timer?

    init() {
        enabled = Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app"
        if enabled {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
    }

    var available: Bool { enabled }

    func post(title: String, body: String) {
        guard enabled else { return }
        pending = (title, body)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { _ in
            MainActor.assumeIsolated { self.flush() }
        }
    }

    private func flush() {
        guard let (title, body) = pending else { return }
        pending = nil
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        let req = UNNotificationRequest(identifier: "brainpoint.gate", content: c, trigger: nil)
        UNUserNotificationCenter.current().add(req) { _ in }
    }
}
