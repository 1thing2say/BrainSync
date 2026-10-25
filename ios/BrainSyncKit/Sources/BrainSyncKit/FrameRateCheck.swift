import Foundation

// The phone's refresh check (INTERFACE-v2-draft.md section 9, "60 Hz, never ProMotion"). The c-VEP
// code is drawn in frames, so it is only the code the bridge decodes when the screen really runs at
// 60 Hz and steadily. The display link asks for exactly 60 Hz, but a ProMotion iPhone that ignores
// it, Low Power Mode or a busy main thread would each distort the code; this check measures what
// actually happens, from the display link's frame timestamps, and says why the phone must not
// flicker (`display_report`). The same idea as ui/flicker_core.js `measure` / `refreshProblem`.
//
// Pure and allocation-free per frame: a fixed ring of the last `window` frame intervals.

public struct FrameRateCheck {
    /// One evaluation of the ring.
    public struct Verdict: Equatable {
        /// The rate from the median frame interval.
        public var measuredHz: Double
        /// The median frame interval (seconds): the period frame indices are rounded to.
        public var framePeriod: Double
        /// Share of intervals more than `unevenFactor` periods off the median.
        public var unevenShare: Double
        public var ok: Bool
        /// Why not ok; "" when ok.
        public var message: String
    }

    public let expectedHz: Double
    public let toleranceHz: Double
    /// Intervals kept (the last ~2 s at 60 Hz).
    public let window: Int
    /// Intervals needed before any verdict (~1 s).
    public let minIntervals: Int
    /// Steady means fewer than this share of uneven intervals (2 %).
    public let maxUnevenShare: Double
    /// An interval is uneven when it is more than this many median periods off the median.
    public let unevenFactor: Double
    /// A gap longer than this (seconds) is a pause (the app was in the background), not a frame.
    public let maxGap: Double

    /// Frames the display link skipped since the last `reset`, from intervals of several periods.
    public private(set) var droppedFrames = 0
    /// Frame timestamps fed since the last `reset`.
    public private(set) var frameCount = 0

    private var ring: [Double]
    private var scratch: [Double]
    private var head = 0
    private var filled = 0
    private var lastTimestamp: Double?

    public init(expectedHz: Double = 60, toleranceHz: Double = 1, window: Int = 120,
                minIntervals: Int = 60, maxUnevenShare: Double = 0.02, unevenFactor: Double = 0.5,
                maxGap: Double = 0.25) {
        self.expectedHz = expectedHz
        self.toleranceHz = toleranceHz
        self.window = max(2, window)
        self.minIntervals = min(max(1, minIntervals), max(2, window))
        self.maxUnevenShare = maxUnevenShare
        self.unevenFactor = unevenFactor
        self.maxGap = maxGap
        ring = Array(repeating: 0, count: max(2, window))
        scratch = ring
    }

    /// One frame's timestamp (seconds, any monotonic clock: CADisplayLink.timestamp).
    public mutating func add(_ timestamp: Double) {
        guard timestamp.isFinite else { return }
        frameCount += 1
        defer { lastTimestamp = timestamp }
        guard let last = lastTimestamp else { return }
        let interval = timestamp - last
        guard interval > 0, interval <= maxGap else { return }
        ring[head] = interval
        head = (head + 1) % ring.count
        filled = min(filled + 1, ring.count)
        let skipped = Int((interval * expectedHz).rounded()) - 1
        if skipped > 0 { droppedFrames += skipped }
    }

    /// Forget every frame (a new measurement, e.g. after the display link restarts).
    public mutating func reset() {
        head = 0
        filled = 0
        lastTimestamp = nil
        droppedFrames = 0
        frameCount = 0
    }

    /// Intervals in the ring.
    public var intervalCount: Int { filled }

    /// The current verdict; nil until `minIntervals` intervals are in.
    public mutating func evaluate() -> Verdict? {
        guard filled >= minIntervals else { return nil }
        for i in 0..<filled { scratch[i] = ring[i] }
        scratch[0..<filled].sort()
        let median = filled % 2 == 1
            ? scratch[filled / 2]
            : (scratch[filled / 2 - 1] + scratch[filled / 2]) / 2
        guard median > 0 else { return nil }
        var uneven = 0
        for i in 0..<filled where abs(scratch[i] - median) > unevenFactor * median { uneven += 1 }
        let share = Double(uneven) / Double(filled)
        let hz = 1 / median
        var message = ""
        if abs(hz - expectedHz) > toleranceHz {
            message = Self.rateProblem(hz: hz, expectedHz: expectedHz)
        } else if share >= maxUnevenShare {
            message = "uneven frames: close other apps"
        }
        return Verdict(measuredHz: hz, framePeriod: median, unevenShare: share,
                       ok: message.isEmpty, message: message)
    }

    /// The measured rate; nil before `minIntervals` intervals.
    public var measuredHz: Double? {
        var copy = self
        return copy.evaluate()?.measuredHz
    }

    /// Whether the phone may flicker: within `toleranceHz` of `expectedHz` and steady.
    public var ok: Bool {
        var copy = self
        return copy.evaluate()?.ok ?? false
    }

    /// Why not ok ("" when ok, "measuring the refresh rate" before a verdict).
    public var message: String {
        var copy = self
        return copy.evaluate()?.message ?? "measuring the refresh rate"
    }

    /// Frames skipped between two consecutive frame indices of a trial (index jumps).
    public static func droppedFrames(from previous: Int, to index: Int) -> Int {
        max(0, index - previous - 1)
    }

    static func rateProblem(hz: Double, expectedHz: Double) -> String {
        let shown = Int(hz.rounded())
        let wanted = Int(expectedHz.rounded())
        if hz > expectedHz * 1.5 {
            return "\(shown) Hz: ProMotion is not capped at \(wanted) Hz"
        }
        if hz < expectedHz {
            return "\(shown) Hz, needs \(wanted) Hz: turn Low Power Mode off"
        }
        return "\(shown) Hz, needs \(wanted) Hz"
    }
}
