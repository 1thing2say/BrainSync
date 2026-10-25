import CoreGraphics
import Foundation

/// Where an item of a screen-scope snapshot comes from. The order of the cases is the order
/// of the hints (see `ScreenScope.assemble`).
public enum ItemKind: String, Sendable, CaseIterable {
    /// An item of an open menu (a menu-bar menu, a context menu or a submenu).
    case menu
    /// A pressable element of the focused window.
    case window
    /// A synthetic "Scroll up" / "Scroll down" strip inside the window's largest scroll area.
    case scroll
    /// A top-level menu-bar item of the front app (pressing it opens its menu).
    case menuBar
    /// A Dock item (an app, or a minimized window); pressing it activates that app.
    case dock
}

/// One pressable element from the AX walk. `frame` is in global screen points with a top-left
/// origin (what AXPosition reports); `id` is its index in the snapshot and its protocol hint id.
/// For a `.scroll` item, `frame` is the synthetic strip (`ScreenScope.scrollStrips`), `role` is
/// "AXScrollArea" and `subrole` "BPScrollUp" or "BPScrollDown".
public struct AXItem: Equatable, Sendable {
    public var id: Int
    public var role: String
    public var subrole: String
    public var label: String
    public var frame: CGRect
    public var enabled: Bool
    public var actions: [String]
    public var kind: ItemKind

    public init(id: Int, role: String, subrole: String, label: String, frame: CGRect,
                enabled: Bool, actions: [String], kind: ItemKind = .window) {
        self.id = id; self.role = role; self.subrole = subrole; self.label = label
        self.frame = frame; self.enabled = enabled; self.actions = actions; self.kind = kind
    }
}

/// The focused window of one app, walked once, plus (screen scope) the front app's menu bar
/// and open menus, the Dock and the scroll strips on the screen that holds the window.
///
/// `screenFrame` is that screen's frame in global top-left points; it is the scan's viewport,
/// so hints and protocol rects are relative to it. A snapshot without one (`.zero`, the
/// window-only walk) uses the window frame as the viewport.
public struct WindowSnapshot: Equatable, Sendable {
    public var appName: String
    public var bundleID: String
    public var pid: Int32
    public var windowTitle: String
    public var windowFrame: CGRect
    public var items: [AXItem]
    public var walkMs: Double
    public var truncated: Bool
    public var screenFrame: CGRect
    /// Per-kind walk times in ms (screen scope only; for logs and bp-probe).
    public var kindMs: [String: Double]

    public init(appName: String, bundleID: String, pid: Int32, windowTitle: String,
                windowFrame: CGRect, items: [AXItem], walkMs: Double, truncated: Bool,
                screenFrame: CGRect = .zero, kindMs: [String: Double] = [:]) {
        self.appName = appName; self.bundleID = bundleID; self.pid = pid
        self.windowTitle = windowTitle; self.windowFrame = windowFrame; self.items = items
        self.walkMs = walkMs; self.truncated = truncated
        self.screenFrame = screenFrame; self.kindMs = kindMs
    }

    /// The scan's viewport in global top-left points: the screen frame when there is one,
    /// else the window frame.
    public var viewport: CGRect {
        screenFrame.width > 0 && screenFrame.height > 0 ? screenFrame : windowFrame
    }

    /// How many items of each kind (for logs and bp-probe).
    public var countsByKind: [ItemKind: Int] {
        Dictionary(grouping: items, by: \.kind).mapValues(\.count)
    }
}

/// A protocol rect, normalized to the scan's viewport (the screen holding the focused window): x right, y down, all in [0, 1].
public struct NormRect: Codable, Equatable, Sendable {
    public var x: Double, y: Double, w: Double, h: Double
    public init(x: Double, y: Double, w: Double, h: Double) { self.x = x; self.y = y; self.w = w; self.h = h }
}

/// What the overlay draws for one layout target, in global screen points (top-left origin).
public struct OverlayTarget: Equatable, Sendable {
    public var slot: String
    public var label: String
    public var kind: String
    public var freq: Double
    public var phase: Double
    public var rect: CGRect

    public init(slot: String, label: String, kind: String, freq: Double, phase: Double, rect: CGRect) {
        self.slot = slot; self.label = label; self.kind = kind; self.freq = freq
        self.phase = phase; self.rect = rect
    }
}

public enum ControlState: String, Sendable {
    case off, dryRun, calibrating, armed, paused
}

/// Everything ControlGate decides from.
public struct GateInputs: Equatable, Sendable {
    public var serverConnected = false
    public var isController = false
    public var boardState = "unknown"
    public var boardOk = false
    public var fakeBoard = false
    public var streamingSince: Double? = nil
    public var axTrusted = false
    public var warningAccepted = false
    public var userPaused = false
    public var safetyPause: String? = nil
    public var phase = "wait"
    public var allowFakeControl = false
    public var frontAppDenied: String? = nil
    /// Why the server holds the session (the phase text, or an error such as
    /// detect_unavailable); used in the reason when the phase is not "run".
    public var serverNote: String? = nil
    public init() {}
}

public struct GateDecision: Equatable, Sendable {
    public var state: ControlState
    public var mayFlicker: Bool
    public var mayPress: Bool
    public var reason: String
    public var headsetLine: String
    /// True on the SIMULATED board without allow-fake-control: picks are shown, never pressed.
    public var dryRun: Bool
    public init(state: ControlState, mayFlicker: Bool, mayPress: Bool, reason: String, headsetLine: String,
                dryRun: Bool = false) {
        self.state = state; self.mayFlicker = mayFlicker; self.mayPress = mayPress
        self.reason = reason; self.headsetLine = headsetLine; self.dryRun = dryRun
    }
}
