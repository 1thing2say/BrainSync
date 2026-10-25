import AppKit
import ApplicationServices
import BrainPointCore

public enum Permissions {
    /// True when this process may use the Accessibility API (System Settings > Privacy &
    /// Security > Accessibility).
    public static var accessibilityTrusted: Bool { AXIsProcessTrusted() }

    /// Shows the system prompt that opens the Accessibility pane (once per process).
    public static func promptAccessibility() {
        // The key behind kAXTrustedCheckOptionPrompt, spelled out: that global is a mutable C
        // var, which Swift 6 refuses to read from nonisolated code.
        _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
    }
}

public struct PressResult: Sendable {
    public var ok: Bool
    public var method: String
    public var detail: String
    public init(ok: Bool, method: String, detail: String) {
        self.ok = ok; self.method = method; self.detail = detail
    }
}

/// Keeps the AXUIElements of the last snapshot by item id and presses them.
///
/// `press` re-reads the element first and refuses when it is gone, belongs to another process
/// than expected (the snapshot's app; the Dock for a Dock item) or to this one, the snapshot's
/// app is no longer frontmost, it is disabled, or its role, label or frame changed (frame by
/// more than 2 pt on any edge). A scroll strip checks its scroll area instead: still an
/// AXScrollArea whose strips (recomputed from its frame) are where the snapshot put them. A
/// Dock item for BrainPoint itself is always refused. Then: text inputs get the keyboard
/// focus; scroll strips scroll by a page (AXScrollUpByPage / AXScrollDownByPage, else the
/// vertical scroll bar's AXDecrement / AXIncrement, else a scroll-wheel event posted to the
/// app over the area); anything else gets AXPress if offered, else AXPick, AXConfirm,
/// AXShowMenu or AXOpen.
public final class ElementRegistry {
    public static let frameTolerance: CGFloat = 2.0

    private var snapshot: WindowSnapshot?
    private var elements: [Int: AXUIElement] = [:]

    public init() {}

    public func replace(with snapshot: WindowSnapshot, elements: [Int: AXUIElement]) {
        self.snapshot = snapshot
        self.elements = elements
    }

    public func clear() {
        snapshot = nil
        elements = [:]
    }

    private func refuse(_ detail: String, method: String = "none") -> PressResult {
        PressResult(ok: false, method: method, detail: detail)
    }

    public func press(id: Int) -> PressResult {
        guard let snap = snapshot, let e = elements[id],
              let item = snap.items.first(where: { $0.id == id }) else {
            return refuse("no element \(id) in the last scan")
        }
        guard let pid = AX.pid(e) else { return refuse("element is gone") }
        if pid == getpid() { return refuse("refused: element belongs to BrainPoint") }
        let expected: Int32? = item.kind == .dock ? ScreenWalker.dockPid() : snap.pid
        if pid != expected {
            return refuse(item.kind == .dock ? "refused: the Dock restarted since the scan"
                                             : "refused: element moved to another app")
        }
        if let front = NSWorkspace.shared.frontmostApplication?.processIdentifier, front != snap.pid {
            return refuse("refused: \(snap.appName) is no longer in front")
        }
        AXUIElementSetMessagingTimeout(e, 0.5)
        if item.kind == .scroll { return pressScroll(e, item: item, snap: snap) }
        guard let v = AX.many(e, [kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute,
                                  kAXDescriptionAttribute, kAXHelpAttribute, kAXEnabledAttribute,
                                  kAXURLAttribute]),
              let role = v[0] as? String else {
            return refuse("element is gone")
        }
        let subrole = v[1] as? String ?? ""
        let label: String
        if item.kind == .dock {
            label = AXLabel.clean(v[2] as? String ?? "")
            if ScreenWalker.isOwnDockItem(url: v[6] as? URL, title: label) {
                return refuse("refused: the Dock item is BrainPoint itself")
            }
        } else {
            label = AXLabel.make(e, role: role, subrole: subrole, title: v[2] as? String,
                                 description: v[3] as? String, help: v[4] as? String)
        }
        guard let frame = AX.frame(e) else { return refuse("element is gone") }
        if role != item.role || label != item.label || !Self.close(frame, item.frame) {
            return refuse("stale: \"\(item.label)\" changed since the scan")
        }
        if AXRoles.isSecure(role: role, subrole: subrole) { return refuse("refused: secure text field") }
        if AX.bool(v[5]) == false { return refuse("\"\(label)\" is disabled") }

        if AXRoles.focusableText.contains(role) {
            let err = AXUIElementSetAttributeValue(e, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            return err == .success
                ? PressResult(ok: true, method: "focus", detail: "focused \"\(label)\"")
                : refuse("focus failed (AXError \(err.rawValue))", method: "focus")
        }
        let actions = AX.actions(e)
        guard let action = AXRoles.usableActions.first(where: { actions.contains($0) }) else {
            return refuse("\"\(label)\" offers no press action")
        }
        let err = AXUIElementPerformAction(e, action as CFString)
        if err == .success { return PressResult(ok: true, method: action, detail: "pressed \"\(label)\"") }
        // Opening a menu-bar menu starts the app's menu tracking, which can hold the reply
        // past the timeout although the menu opened.
        if item.kind == .menuBar, err == .cannotComplete {
            return PressResult(ok: true, method: action, detail: "pressed \"\(label)\" (menu opening; the app did not reply in time)")
        }
        return refuse("\(action) failed (AXError \(err.rawValue))", method: action)
    }

    /// A scroll strip: the area must still be a scroll area with the strip where it was.
    private func pressScroll(_ area: AXUIElement, item: AXItem, snap: WindowSnapshot) -> PressResult {
        guard AX.string(area, kAXRoleAttribute) == ScreenScope.scrollRole,
              let s = ScreenWalker.strips(area: area, window: snap.windowFrame, screen: snap.viewport) else {
            return refuse("scroll area is gone")
        }
        let up = item.subrole == ScreenScope.scrollUpTag
        if !Self.close(up ? s.up : s.down, item.frame) {
            return refuse("stale: the scroll area moved since the scan")
        }
        let (method, bar) = ScreenWalker.scrollMethod(area)
        switch method {
        case .byPage:
            let a = up ? "AXScrollUpByPage" : "AXScrollDownByPage"
            let err = AXUIElementPerformAction(area, a as CFString)
            if err == .success { return PressResult(ok: true, method: a, detail: "scrolled \(up ? "up" : "down") a page") }
        case .scrollBar:
            if let bar {
                let a = up ? "AXDecrement" : "AXIncrement"
                AXUIElementSetMessagingTimeout(bar, 0.5)
                let err = AXUIElementPerformAction(bar, a as CFString)
                if err == .success {
                    return PressResult(ok: true, method: a, detail: "scrolled \(up ? "up" : "down") (scroll bar)")
                }
            }
        case .wheel:
            break
        }
        // Last resort: a scroll-wheel event over the area, posted to the app only (the mouse
        // pointer does not move). Positive deltas scroll toward the top.
        guard let f = AX.frame(area), let pid = AX.pid(area) else { return refuse("scroll area is gone") }
        let amount = Int32(max(40, min(2000, f.height * 0.8)))
        guard let ev = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1,
                               wheel1: up ? amount : -amount, wheel2: 0, wheel3: 0) else {
            return refuse("could not make a scroll event", method: ScreenWalker.ScrollMethod.wheel.rawValue)
        }
        ev.location = CGPoint(x: f.midX, y: f.midY)
        ev.postToPid(pid)
        return PressResult(ok: true, method: ScreenWalker.ScrollMethod.wheel.rawValue,
                           detail: "scrolled \(up ? "up" : "down") (scroll-wheel event)")
    }

    static func close(_ a: CGRect, _ b: CGRect) -> Bool {
        let t = frameTolerance
        return abs(a.minX - b.minX) <= t && abs(a.minY - b.minY) <= t
            && abs(a.maxX - b.maxX) <= t && abs(a.maxY - b.maxY) <= t
    }
}
