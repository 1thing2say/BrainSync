import Foundation

/// The suite's three c-VEP targets. The phone is a slots-mode controller with three
/// position-only slots, the same m-sequence at three lags (docs/CVEP-STANDARD.md). Each app
/// decides what a slot means, so switching apps needs no new hello and the template trained
/// once stays valid.
public enum SuiteSlots {
    /// Slot ids in screen order; index i is the i-th square.
    public static let ids = ["t0", "t1", "t2"]
    /// Each slot's lag in frames, by index: t0 0, t1 21, t2 42.
    public static let lags = [0, 21, 42]
    public static var count: Int { ids.count }

    /// The slot's index (0, 1, 2), or nil for an id the suite does not use.
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

/// The shape every app core follows, so the view that draws the three squares and the code
/// that receives the decoder's selection treat every app the same way. A core is pure: it
/// takes a selected slot id and returns what the app should do; the views, the camera and the
/// decoder stay out of it.
public protocol SuiteAppCore {
    associatedtype Effect
    /// One label per slot (t0, t1, t2): what each square says right now.
    var labels: [String] { get }
    /// One flag per slot: false means a look at that square does nothing now, and the view dims
    /// its label. The square keeps flickering, because the decoder always chooses among all three.
    var enabled: [Bool] { get }
    /// What t0 is called while the decoder trains (the user looks at t0 for 30 cycles).
    var trainingLabel: String { get }
    /// Handle a selection. Returns nil when nothing happens: an unknown slot id or a disabled
    /// square. No cooldown here: the server already keeps 1.5 s between selections (PROTOCOL-v3
    /// section 6).
    mutating func select(_ slot: String) -> Effect?
}

/// The Home screen: t0 Camera, t1 Reels, t2 Voice.
public struct SuiteHomeCore: SuiteAppCore, Equatable, Sendable {
    public typealias Effect = SuiteApp

    /// The app behind each slot, in slot order.
    public static let apps: [SuiteApp] = [.camera, .reels, .voice]

    public init() {}

    public var labels: [String] { Self.apps.map(\.title) }
    public var enabled: [Bool] { [true, true, true] }
    public var trainingLabel: String { labels[0] }

    /// The app to open, or nil for an unknown slot.
    public mutating func select(_ slot: String) -> SuiteApp? {
        SuiteSlots.index(of: slot).map { Self.apps[$0] }
    }
}
