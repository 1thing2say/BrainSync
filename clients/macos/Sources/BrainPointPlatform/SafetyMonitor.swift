import AppKit
import Carbon

/// Reports when the screen is not safe to flicker on or press into: the screen is locked, the
/// screensaver runs, the displays or the system sleep, the user session is switched away, or
/// secure keyboard entry is on (a password field has focus somewhere; polled at 2 Hz).
/// `onChange` gets one human reason, or nil when it is safe again; it is called only on a
/// change, and once from `start()` when something is already unsafe. Call `stop()` before
/// releasing it.
@MainActor
public final class SafetyMonitor {
    private let onChange: @MainActor (String?) -> Void
    private var active: Set<Reason> = []
    private var reported: String?? = .none   // .none = nothing reported yet
    private var tokens: [(NotificationCenter, NSObjectProtocol)] = []
    private var timer: Timer?

    /// In order of priority: the first active one is reported.
    private enum Reason: Int, CaseIterable, Comparable {
        case sessionInactive, systemSleep, screensAsleep, screenLocked, screensaver, secureInput
        static func < (a: Reason, b: Reason) -> Bool { a.rawValue < b.rawValue }
        var text: String {
            switch self {
            case .sessionInactive: return "user session switched away"
            case .systemSleep: return "system asleep"
            case .screensAsleep: return "displays asleep"
            case .screenLocked: return "screen locked"
            case .screensaver: return "screensaver running"
            case .secureInput: return "secure input is on (a password field has focus)"
            }
        }
    }

    public init(onChange: @escaping @MainActor (String?) -> Void) {
        self.onChange = onChange
    }

    /// The current reason, or nil when safe.
    public var reason: String? { active.min()?.text }

    public func start() {
        guard tokens.isEmpty else { return }
        let dnc = DistributedNotificationCenter.default()
        observe(dnc, "com.apple.screenIsLocked", set: .screenLocked, on: true)
        observe(dnc, "com.apple.screenIsUnlocked", set: .screenLocked, on: false)
        observe(dnc, "com.apple.screensaver.didstart", set: .screensaver, on: true)
        observe(dnc, "com.apple.screensaver.didstop", set: .screensaver, on: false)
        let wnc = NSWorkspace.shared.notificationCenter
        observe(wnc, NSWorkspace.screensDidSleepNotification.rawValue, set: .screensAsleep, on: true)
        observe(wnc, NSWorkspace.screensDidWakeNotification.rawValue, set: .screensAsleep, on: false)
        observe(wnc, NSWorkspace.willSleepNotification.rawValue, set: .systemSleep, on: true)
        observe(wnc, NSWorkspace.didWakeNotification.rawValue, set: .systemSleep, on: false)
        observe(wnc, NSWorkspace.sessionDidResignActiveNotification.rawValue, set: .sessionInactive, on: true)
        observe(wnc, NSWorkspace.sessionDidBecomeActiveNotification.rawValue, set: .sessionInactive, on: false)

        if Self.screenLockedNow() { active.insert(.screenLocked) }
        if IsSecureEventInputEnabled() { active.insert(.secureInput) }
        let t = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.pollSecureInput() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        if !active.isEmpty { report() }
    }

    public func stop() {
        for (nc, t) in tokens { nc.removeObserver(t) }
        tokens = []
        timer?.invalidate()
        timer = nil
    }

    // MARK: - private

    private func observe(_ nc: NotificationCenter, _ name: String, set r: Reason, on: Bool) {
        let t = nc.addObserver(forName: Notification.Name(name), object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.set(r, on) }
        }
        tokens.append((nc, t))
    }

    private func set(_ r: Reason, _ on: Bool) {
        if on { active.insert(r) } else { active.remove(r) }
        // Waking up or unlocking also clears what may have been missed while asleep.
        if !on && (r == .systemSleep || r == .screensAsleep) && !Self.screenLockedNow() {
            active.remove(.screenLocked)
        }
        report()
    }

    private func pollSecureInput() {
        let on = IsSecureEventInputEnabled()
        if on != active.contains(.secureInput) { set(.secureInput, on) }
    }

    private func report() {
        let now = reason
        if case .some(let last) = reported, last == now { return }
        reported = .some(now)
        onChange(now)
    }

    /// The session dictionary's lock flag (set while the lock screen shows).
    static func screenLockedNow() -> Bool {
        guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return (d["CGSSessionScreenIsLocked"] as? Bool) ?? false
    }
}

/// Esc anywhere: an NSEvent global monitor (other apps in front; needs Accessibility trust)
/// plus a local one (our own windows). Esc is passed on, not swallowed. Call `stop()` before
/// releasing it.
@MainActor
public final class EscapeMonitor {
    private let onEscape: @MainActor () -> Void
    private var global: Any?
    private var local: Any?
    private static let escKeyCode: UInt16 = 53

    public init(onEscape: @escaping @MainActor () -> Void) {
        self.onEscape = onEscape
    }

    public func start() {
        guard global == nil else { return }
        global = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard e.keyCode == Self.escKeyCode else { return }
            MainActor.assumeIsolated { self?.onEscape() }
        }
        local = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            if e.keyCode == Self.escKeyCode {
                MainActor.assumeIsolated { self?.onEscape() }
            }
            return e
        }
    }

    public func stop() {
        if let g = global { NSEvent.removeMonitor(g) }
        if let l = local { NSEvent.removeMonitor(l) }
        global = nil
        local = nil
    }
}
