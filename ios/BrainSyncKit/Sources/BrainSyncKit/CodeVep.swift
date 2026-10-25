import Foundation

/// The BrainSync c-VEP standard (docs/CVEP-STANDARD.md), ported from clients/js/cvep.js so the
/// phone draws exactly what the browser and the PsychoPy display draw: three white squares on
/// black showing one 63-bit m-sequence, target k delayed by k × 21 frames. Frame n of a target
/// at lag L is white when `bits[(n - L) mod 63]` is 1, black when 0; n counts frames since the
/// layout's first flickering frame, so a cycle starts on every n with n mod 63 == 0.
///
/// The text helpers (`bar`, `labelLine`, `statusLine`, `trainingLine`, `trainedLine`) give the
/// confidence lines under the squares byte for byte as cvep.js and `ConfidenceState` in
/// clients/js/brainsync-client.js do, including JavaScript's rounding (`Math.round` and
/// `toFixed` round a tie up, where Python and printf round it to even), so the phone, the web
/// page and the extension never disagree on a cell or a digit. Tests check it against both
/// (Fixtures/cvep_frames.json, written by tools/ios_cvep_fixture.py).
public enum CodeVep {
    /// The standard's code: `brainsync.vep.codes.m_sequence(63)` (LFSR x^6 + x + 1, seed 1).
    public static let bits: [UInt8] = [
        0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1,
        1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0, 0, 0,
        1, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1,
    ]
    public static let length = 63
    public static let targetCount = 3
    /// 63 / 3: the widest spacing three targets allow (350 ms at 60 Hz).
    public static let lagFrames = 21
    /// One frame per bit: 1.05 s at 60 Hz.
    public static let cycleFrames = 63
    /// Cycles recorded on target 0 before the template is ready (about 32 s).
    public static let trainCycles = 30
    public static let barCells = 8
    /// The lag of target k (0, 21, 42 frames).
    public static func lag(ofTarget k: Int) -> Int { k * lagFrames }

    // MARK: - The code a server sends

    /// The welcome's `code` object (PROTOCOL-v3 section 6): the bits the decoder's template was
    /// trained on. Decodes the server's JSON as it is (`lag_frames`, `cycle_frames`, ...).
    public struct Code: Codable, Equatable, Sendable {
        public var bits: [UInt8]
        public var lagFrames: Int
        public var cycleFrames: Int
        public var trainCycles: Int?
        public var fill: String

        enum CodingKeys: String, CodingKey {
            case bits, fill
            case lagFrames = "lag_frames"
            case cycleFrames = "cycle_frames"
            case trainCycles = "train_cycles"
        }

        public init(bits: [UInt8], lagFrames: Int, cycleFrames: Int, trainCycles: Int?, fill: String = "#ffffff") {
            self.bits = bits
            self.lagFrames = lagFrames
            self.cycleFrames = cycleFrames
            self.trainCycles = trainCycles
            self.fill = fill
        }

        /// The standard: 63 bits, 21-frame lags, 30 training cycles, white.
        public static let standard = Code(bits: CodeVep.bits, lagFrames: CodeVep.lagFrames,
                                          cycleFrames: CodeVep.cycleFrames,
                                          trainCycles: CodeVep.trainCycles, fill: "#ffffff")

        /// True when the server's code can be drawn: bits all 0 or 1, a positive lag, and one
        /// frame per bit (`cycle_frames` == bit count), as `parseCode` in brainsync-client.js.
        public var isValid: Bool {
            !bits.isEmpty && bits.allSatisfy { $0 <= 1 } && lagFrames > 0 && cycleFrames == bits.count
        }

        /// The welcome's code when it is valid, else the standard (what the browser does).
        public static func orStandard(_ code: Code?) -> Code {
            guard let code, code.isValid else { return .standard }
            return code
        }
    }

    // MARK: - Frame rule

    /// True (white) when the target at `lag` frames is lit on flicker frame `frame`:
    /// `bits[(frame - lag) mod bits.count]` is 1. Negative frames wrap like the JS `mod`.
    public static func isLit(bits: [UInt8] = CodeVep.bits, frame: Int, lag: Int) -> Bool {
        let count = bits.count
        guard count > 0 else { return true }
        var i = (frame - lag) % count
        if i < 0 { i += count }
        return bits[i] != 0
    }

    /// 1 (white) or 0 (black): target `target` on frame `frame`, as cvep.js `bit`.
    public static func bit(target: Int, frame: Int, lagFrames: Int = CodeVep.lagFrames,
                           bits: [UInt8] = CodeVep.bits) -> Int {
        isLit(bits: bits, frame: frame, lag: target * lagFrames) ? 1 : 0
    }

    /// True on each cycle's first frame (0, 63, 126, ...): the frame whose flip is reported.
    public static func isCycleStart(frame: Int, cycleFrames: Int = CodeVep.cycleFrames) -> Bool {
        guard cycleFrames > 0 else { return false }
        var m = frame % cycleFrames
        if m < 0 { m += cycleFrames }
        return m == 0
    }

    /// The cycle number of frame `frame` (floor division).
    public static func cycleIndex(frame: Int, cycleFrames: Int = CodeVep.cycleFrames) -> Int {
        guard cycleFrames > 0 else { return 0 }
        return Int((Double(frame) / Double(cycleFrames)).rounded(.down))
    }

    // MARK: - Confidence text

    /// `cells` characters filled in proportion to `value` clipped to 0..1: "███░░░░░".
    /// A non-finite value shows empty. Scores are correlations, not probabilities.
    public static func bar(_ value: Double, cells: Int = CodeVep.barCells) -> String {
        let v = value.isFinite ? min(1, max(0, value)) : 0
        let n = min(cells, max(0, Int(jsRound(v * Double(cells)))))
        return String(repeating: "█", count: n) + String(repeating: "░", count: cells - n)
    }

    /// Width of a graphical meter in 0...1 (a negative or missing score shows empty).
    public static func level(_ score: Double) -> Double {
        score.isFinite ? min(1, max(0, score)) : 0
    }

    /// The score as the label line shows it: "+0.42", "-0.03", or "  -  " when non-finite.
    public static func scoreText(_ score: Double) -> String {
        guard score.isFinite else { return "  -  " }
        return (score >= 0 ? "+" : "") + toFixed2(score)
    }

    /// "YES  ███░░░░░ +0.42 <": the label padded to `width`, the bar, the signed score, and
    /// " <" on this decision's best target (cvep.js `labelLine`). `width` defaults to the
    /// label's own length; pass the longest label's so the bars line up.
    public static func labelLine(label: String, score: Double, best: Bool, width: Int? = nil) -> String {
        let pad = max(0, (width ?? label.utf16.count) - label.utf16.count)
        return label + String(repeating: " ", count: pad) + " " + bar(score) + " " + scoreText(score)
            + (best ? " <" : "")
    }

    /// One label line per target, padded to the longest label, as `ConfidenceState.lines`:
    /// `labels` are shown as given (the apps upper-case them), `scores[k]` nil or non-finite
    /// shows "  -  ", and `best` (the decision's `raw`) gets " <".
    public static func labelLines(labels: [String], scores: [Double?], best: Int?) -> [String] {
        let width = labels.map(\.utf16.count).max() ?? 0
        return labels.enumerated().map { k, label in
            let s = k < scores.count ? (scores[k] ?? .nan) : .nan
            return labelLine(label: label, score: s, best: k == best, width: width)
        }
    }

    /// "lead 0.31   choice: YES": the lead over the runner-up ("lead  -  " when unknown) and
    /// the committed label ("no choice yet" when nil), " *" on the decision that changed it.
    public static func statusLine(lead: Double?, committed: String?, changed: Bool) -> String {
        let gap: String
        if let lead, lead.isFinite { gap = "lead " + toFixed2(lead) } else { gap = "lead  -  " }
        let choice = committed.map { "choice: " + $0 + (changed ? " *" : "") } ?? "no choice yet"
        return gap + "   " + choice
    }

    /// "training: keep looking at YES   cycle 12/30" (the `train` phase; `cycle` 0 before the
    /// first `train` progress message).
    public static func trainingLine(cue: String, cycle: Int, of total: Int) -> String {
        "training: keep looking at \(cue)   cycle \(cycle)/\(total)"
    }

    /// "template ready: 83 % on held-out cycles (chance 33 %)" (the `trained` message; "?" for a
    /// missing accuracy or chance).
    public static func trainedLine(accuracy: Double?, chance: Double?) -> String {
        func pct(_ v: Double?) -> String {
            guard let v, v.isFinite else { return "?" }
            return String(Int(jsRound(100 * v)))
        }
        return "template ready: \(pct(accuracy)) % on held-out cycles (chance \(pct(chance)) %)"
    }

    // MARK: - JavaScript rounding

    /// `Math.round`: the nearest integer, a tie toward +infinity (Swift's `.toNearestOrEven`
    /// and Python's `round` would turn 0.5 into 0).
    static func jsRound(_ x: Double) -> Double {
        guard x.isFinite else { return x }
        let down = x.rounded(.down)
        return x - down >= 0.5 ? down + 1 : down   // x - down is exact for every finite double
    }

    /// `Number.prototype.toFixed(2)` for finite values: the exact decimal value rounded to two
    /// places, a tie away from zero (printf rounds it to even: 0.125 is "0.12" there, "0.13" in
    /// JS). A tie at the third decimal needs x × 8 to be an odd integer, which is exact to test.
    static func toFixed2(_ x: Double) -> String {
        guard x.isFinite else { return String(x) }
        let negative = x < 0           // -0 is not < 0: JS prints it "0.00"
        let a = abs(x)
        let eighths = a * 8            // exact: a power-of-two scale
        let text: String
        if eighths == eighths.rounded(.down), eighths.truncatingRemainder(dividingBy: 2) == 1, a < 1e15 {
            // An exact tie x = m / 8 with m odd: a * 100 = 12.5 m is exact, round its half up.
            let hundredths = (a * 100).rounded(.down) + 1
            let whole = Int(hundredths) / 100
            let frac = Int(hundredths) % 100
            text = "\(whole)." + (frac < 10 ? "0" : "") + "\(frac)"
        } else {
            text = String(format: "%.2f", a)
        }
        return (negative ? "-" : "") + text
    }
}
