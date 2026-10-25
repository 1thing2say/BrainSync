import Foundation

/// The suite's two c-VEP targets: never more than two squares (docs/CVEP-STANDARD.md). The
/// phone is a slots-mode controller with two position-only slots, the same m-sequence at two
/// lags, 31 frames apart (the widest two targets allow in a 63-frame cycle). Each app decides
/// what a slot means, so switching apps needs no new hello and the template trained once stays
/// valid.
public enum SuiteSlots {
    /// Slot ids in screen order; index i is the i-th square.
    public static let ids = ["t0", "t1"]
    /// Each slot's lag in frames, by index: t0 0, t1 31.
    public static let lags = [0, CodeVep.lagFrames]
    public static var count: Int { ids.count }

    /// The slot's index (0, 1), or nil for an id the suite does not use.
    public static func index(of slot: String) -> Int? { ids.firstIndex(of: slot) }

    /// The slot's lag in frames, or nil for an id the suite does not use.
    public static func lag(of slot: String) -> Int? { index(of: slot).map { lags[$0] } }
}

/// The apps in the suite. Home picks one of the other three.
public enum SuiteApp: String, CaseIterable, Equatable, Sendable {
    case home, camera, reels, voice

    /// The name shown on screen.
    public var title: String {
        switch self {
        case .home: return "Home"
        case .camera: return "Camera"
        case .reels: return "Reels"
        case .voice: return "Voice"
        }
    }
}

/// The shape every app core follows, so the view that draws the two squares and the code that
/// receives the decoder's selection treat every app the same way. A core is pure: it takes a
/// selected slot id and returns what the app should do; the views, the camera and the decoder
/// stay out of it.
public protocol SuiteAppCore {
    associatedtype Effect
    /// One label per slot (t0, t1): what each square says right now.
    var labels: [String] { get }
    /// One flag per slot: false means a look at that square does nothing now, and the view dims
    /// its label. The square keeps flickering, because the decoder always chooses between both.
    var enabled: [Bool] { get }
    /// What t0 is called while the decoder trains (the user looks at t0 for 30 cycles).
    var trainingLabel: String { get }
    /// Handle a selection. Returns nil when nothing happens: an unknown slot id or a disabled
    /// square. No cooldown here: the server already keeps 1.5 s between selections (PROTOCOL-v3
    /// section 6).
    mutating func select(_ slot: String) -> Effect?
}

/// The Home screen with two squares: t0 opens the highlighted app, t1 moves the highlight to
/// the next one (Camera, Reels, Voice, then Camera again). A misread Next only moves the
/// highlight, so that error never opens an app.
public struct SuiteHomeCore: SuiteAppCore, Equatable, Sendable {
    /// What the app does after a selection.
    public enum Effect: Equatable, Sendable {
        /// Open this app.
        case open(SuiteApp)
        /// The highlight moved to this app.
        case highlight(SuiteApp)
    }

    /// The apps Home offers, in the order Next steps through them.
    public static let apps: [SuiteApp] = [.camera, .reels, .voice]

    /// The highlighted app's index in `apps`.
    public private(set) var highlighted = 0

    public init() {}

    /// The app t0 opens now.
    public var current: SuiteApp { Self.apps[highlighted] }

    /// Highlight `app` (for example the app just left); apps Home does not offer are ignored.
    public mutating func highlight(_ app: SuiteApp) {
        if let i = Self.apps.firstIndex(of: app) { highlighted = i }
    }

    public var labels: [String] { ["Open \(current.title)", "Next"] }
    public var enabled: [Bool] { [true, true] }
    public var trainingLabel: String { labels[0] }

    /// t0: open the highlighted app; t1: highlight the next one; nil for an unknown slot.
    public mutating func select(_ slot: String) -> Effect? {
        switch SuiteSlots.index(of: slot) {
        case 0:
            return .open(current)
        case 1:
            highlighted = (highlighted + 1) % Self.apps.count
            return .highlight(current)
        default:
            return nil
        }
    }
}
