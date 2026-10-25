import Foundation

/// The BrainSync c-VEP standard (docs/CVEP-STANDARD.md): a port of `clients/js/cvep.js` and
/// `brainsync/vep/live.py` for the desktop overlay.
///
/// Three white squares on black show one 63-bit m-sequence (`brainsync.vep.codes.m_sequence(63)`),
/// target k delayed by k * 21 frames. Frame n of a target with lag L (in frames) is white when
/// `bits[(n - L) mod 63]` is 1 and black when 0, with n counted from the layout's first
/// flickering frame (one bit per frame); a still square is drawn white. A cycle starts on every
/// n with n mod 63 == 0, and its first flip's time goes to the server as `cycle`.
public enum CodeVEP {
    /// `000001111110101011001101110110100100111000101111001010001100001`
    public static let standardBits: [UInt8] = [
        0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1,
        1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0, 0, 0,
        1, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1]
    public static let nTargets = 3
    public static let lagFrames = 21
    public static let cycleFrames = 63
    public static let trainCycles = 30
    public static let barCells = 8
    public static let fill = "#ffffff"

    static func mod(_ a: Int, _ b: Int) -> Int {
        let m = a % b
        return m < 0 ? m + b : m
    }

    /// 1 (white) or 0 (black): a target with code lag `lag` on flicker frame `frame`.
    public static func bit(lag: Int, frame: Int, bits: [UInt8] = standardBits) -> UInt8 {
        guard !bits.isEmpty else { return 1 }
        return bits[mod(frame - lag, bits.count)]
    }

    /// True on each cycle's first frame (0, 63, 126, ...).
    public static func cycleStart(_ frame: Int, cycleFrames: Int = cycleFrames) -> Bool {
        cycleFrames > 0 && mod(frame, cycleFrames) == 0
    }

    /// The cycle number of frame `frame` (floor division).
    public static func cycleIndex(_ frame: Int, cycleFrames: Int = cycleFrames) -> Int {
        guard cycleFrames > 0 else { return 0 }
        return frame >= 0 ? frame / cycleFrames : -((-frame + cycleFrames - 1) / cycleFrames)
    }

    // MARK: confidence text (CVEP-STANDARD section 3)

    /// `cells` characters filled in proportion to `value` clipped to 0..1 (a correlation).
    /// Rounds half to even, as Python's `round` in brainsync.vep.live.bar (cvep.js's Math.round
    /// rounds an exact half cell up, so the two differ at multiples of 1/16 only).
    public static func bar(_ value: Double, cells: Int = barCells) -> String {
        let v = value.isFinite ? min(1, max(0, value)) : 0
        let n = Int((v * Double(cells)).rounded(.toNearestOrEven))
        return String(repeating: "█", count: n) + String(repeating: "░", count: cells - n)
    }

    /// The signed score with two decimals, "  -  " when it is not finite.
    static func signed(_ s: Double) -> String {
        guard s.isFinite else { return "  -  " }
        return (s >= 0 ? "+" : "") + String(format: "%.2f", s)
    }

    /// "YES  ███░░░░░ +0.42 <": the label padded to `width`, the bar, the score, "<" on the best.
    public static func labelLine(_ label: String, score: Double, best: Bool, width: Int? = nil) -> String {
        let w = width ?? label.count
        let padded = label.count >= w ? label : label + String(repeating: " ", count: w - label.count)
        return padded + " " + bar(score) + " " + signed(score) + (best ? " <" : "")
    }

    /// "lead 0.31   choice: YES *" ("no choice yet" before the first commit).
    public static func statusLine(lead: Double?, committed: String?, changed: Bool) -> String {
        let gap = lead.map { $0.isFinite ? String(format: "lead %.2f", $0) : "lead  -  " } ?? "lead  -  "
        let choice = committed.map { "choice: " + $0 + (changed ? " *" : "") } ?? "no choice yet"
        return gap + "   " + choice
    }

    /// "template ready: 83 % on held-out cycles (chance 33 %)", as brainsync.vep.
    public static func trainedLine(accuracy: Double?, chance: Double?) -> String {
        guard let a = accuracy, a.isFinite else { return "template ready" }
        var s = "template ready: \(Int((100 * a).rounded())) % on held-out cycles"
        if let c = chance, c.isFinite { s += " (chance \(Int((100 * c).rounded())) %)" }
        return s
    }

    /// "Look at the Sign in patch   cycle 12/30": the line of the train phase.
    public static func trainLine(text: String, cueLabel: String?, cycle: Int?, of: Int) -> String {
        let look = text.isEmpty ? "Look at the \(cueLabel ?? "first") patch" : text
        return look + "   cycle \(max(0, cycle ?? 0))/\(of)"
    }
}

/// The c-VEP code a server announced in its welcome (`paradigm: "code"`, CVEP-STANDARD 4.2).
public struct CodeSpec: Equatable, Sendable {
    public var bits: [UInt8]
    public var lagFrames: Int
    public var cycleFrames: Int
    public var trainCycles: Int
    public var fill: String
    /// False when the welcome's bits were missing or not all 0 / 1 (the standard bits are used).
    public var bitsFromServer: Bool

    public init(bits: [UInt8] = CodeVEP.standardBits, lagFrames: Int = CodeVEP.lagFrames,
                cycleFrames: Int? = nil, trainCycles: Int = CodeVEP.trainCycles, fill: String = CodeVEP.fill,
                bitsFromServer: Bool = true) {
        self.bits = bits.isEmpty ? CodeVEP.standardBits : bits
        self.lagFrames = lagFrames
        self.cycleFrames = cycleFrames ?? self.bits.count
        self.trainCycles = trainCycles
        self.fill = fill
        self.bitsFromServer = bitsFromServer
    }

    public static let standard = CodeSpec()

    public var isStandard: Bool { bits == CodeVEP.standardBits && cycleFrames == CodeVEP.cycleFrames }

    /// The fill colour's red, green and blue in 0...1 (white when `fill` is not "#rrggbb").
    public var fillRGB: (Double, Double, Double) {
        let hex = fill.hasPrefix("#") ? String(fill.dropFirst()) : ""
        guard hex.count == 6, let v = UInt32(hex, radix: 16) else { return (1, 1, 1) }
        return (Double((v >> 16) & 0xff) / 255, Double((v >> 8) & 0xff) / 255, Double(v & 0xff) / 255)
    }
}

/// Which cycle reports are due, for one layout's flicker (pure, so the overlay and the tests
/// share it). Call `due` on every flickering frame; it returns the cycle to report once per
/// cycle: on its first frame, or on the next frame drawn when that first frame was dropped,
/// with the time the first frame was due (t0 + first * period).
public struct CycleCounter: Equatable, Sendable {
    public private(set) var layoutID: Int?
    public private(set) var t0: Double?
    public private(set) var last = -1

    public init() {}

    public mutating func reset() { layoutID = nil; t0 = nil; last = -1 }

    /// (cycle number, flip time in the frame's clock) due on frame `n` at time `t`, or nil.
    public mutating func due(layoutID: Int, t0: Double, frame n: Int, t: Double, period: Double,
                             cycleFrames: Int) -> (n: Int, t: Double)? {
        guard cycleFrames > 0, n >= 0 else { return nil }
        if self.layoutID != layoutID || self.t0 != t0 {
            self.layoutID = layoutID; self.t0 = t0; last = -1
        }
        let c = CodeVEP.cycleIndex(n, cycleFrames: cycleFrames)
        guard c > last else { return nil }
        last = c
        let first = c * cycleFrames
        return (c, n == first ? t : t0 + Double(first) * period)
    }
}
