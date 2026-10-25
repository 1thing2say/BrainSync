import AppKit
import ApplicationServices

public enum FocusEvent: Sendable {
    /// Another app came to the front.
    case activated(pid: Int32, bundleID: String, name: String)
    /// The front app's focused window changed, moved, resized, was created or was destroyed.
    case windowChanged(pid: Int32)
    /// Nothing usable in front (no app, or BrainPoint itself).
    case none
}

/// Watches which app is in front (NSWorkspace activation) and, through an AXObserver on that
/// app, its focused window. The observer moves to each newly activated app. `start()` reports
/// the current front app at once. Events are delivered on the main thread, not debounced.
/// Call `stop()` before releasing it (the AXObserver holds an unretained pointer to it).
@MainActor
public final class FocusWatcher {
    private let onEvent: @MainActor (FocusEvent) -> Void
    private var activationToken: NSObjectProtocol?
    private var observer: AXObserver?
    private var observedPid: Int32 = 0
    private var observedApp: AXUIElement?
    private var observedWindow: AXUIElement?

    private static let appNotifications = [
        kAXFocusedWindowChangedNotification, kAXMainWindowChangedNotification,
        kAXWindowCreatedNotification, kAXWindowMovedNotification, kAXWindowResizedNotification,
        kAXWindowMiniaturizedNotification, kAXApplicationHiddenNotification,
    ]

    public init(onEvent: @escaping @MainActor (FocusEvent) -> Void) {
        self.onEvent = onEvent
    }

    public func start() {
        guard activationToken == nil else { return }
        activationToken = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            let pid = app?.processIdentifier
            MainActor.assumeIsolated { self?.frontChanged(pid: pid) }
        }
        frontChanged(pid: NSWorkspace.shared.frontmostApplication?.processIdentifier)
    }

    public func stop() {
        if let t = activationToken { NSWorkspace.shared.notificationCenter.removeObserver(t) }
        activationToken = nil
        detach()
    }

    // MARK: - private

    private func frontChanged(pid: Int32?) {
        guard let pid, pid != getpid(), let app = NSRunningApplication(processIdentifier: pid) else {
            detach()
            onEvent(.none)
            return
        }
        if pid != observedPid { attach(pid: pid) }
        onEvent(.activated(pid: pid, bundleID: app.bundleIdentifier ?? "", name: app.localizedName ?? ""))
    }

    private func attach(pid: Int32) {
        detach()
        var obs: AXObserver?
        let callback: AXObserverCallback = { _, element, notification, refcon in
            guard let refcon else { return }
            let watcher = Unmanaged<FocusWatcher>.fromOpaque(refcon).takeUnretainedValue()
            let name = notification as String
            // Runs on the main run loop, where the source was added.
            MainActor.assumeIsolated { watcher.axEvent(name, element: element) }
        }
        guard AXObserverCreate(pid, callback, &obs) == .success, let obs else { return }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for n in Self.appNotifications {
            _ = AXObserverAddNotification(obs, app, n as CFString, refcon)
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .commonModes)
        observer = obs
        observedPid = pid
        observedApp = app
        watchFocusedWindow()
    }

    private func detach() {
        if let obs = observer {
            if let app = observedApp {
                for n in Self.appNotifications { AXObserverRemoveNotification(obs, app, n as CFString) }
            }
            if let w = observedWindow {
                AXObserverRemoveNotification(obs, w, kAXUIElementDestroyedNotification as CFString)
            }
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .commonModes)
        }
        observer = nil
        observedPid = 0
        observedApp = nil
        observedWindow = nil
    }

    /// UIElementDestroyed only arrives when registered on the element itself, so it follows
    /// the focused window.
    private func watchFocusedWindow() {
        guard let obs = observer, let app = observedApp else { return }
        if let w = observedWindow {
            AXObserverRemoveNotification(obs, w, kAXUIElementDestroyedNotification as CFString)
        }
        observedWindow = AX.element(app, kAXFocusedWindowAttribute)
        if let w = observedWindow {
            let refcon = Unmanaged.passUnretained(self).toOpaque()
            _ = AXObserverAddNotification(obs, w, kAXUIElementDestroyedNotification as CFString, refcon)
        }
    }

    private func axEvent(_ name: String, element: AXUIElement) {
        guard observer != nil, observedPid != 0 else { return }
        // Window events right after our own enableAppAccessibility write are ours, not the user's.
        if EnableLog.shared.isQuiet(observedPid) { return }
        if name == kAXFocusedWindowChangedNotification || name == kAXMainWindowChangedNotification
            || name == kAXWindowCreatedNotification || name == kAXUIElementDestroyedNotification {
            watchFocusedWindow()
        }
        onEvent(.windowChanged(pid: observedPid))
    }
}
