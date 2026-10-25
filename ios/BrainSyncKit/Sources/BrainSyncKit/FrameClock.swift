import Foundation

/// Frame counting for the c-VEP flicker, from display-link timestamps (media time, seconds),
/// as `FlickerState` in clients/js/brainsync-client.js does it from requestAnimationFrame.
///
/// Why from timestamps and not a frame counter: the code shows one bit a frame, and the decoder
/// lines its template up with the cycle times the phone reports. A counter that adds one per
/// callback slips a bit behind on every dropped frame and stays behind; here frame n is
/// `round((t - t0) / period)`, so a late or dropped frame skips its bits and the next frame is
/// back on the code (the PsychoPy display and the browser do the same). The period is the
/// nominal refresh (60 Hz) unless the measured refresh is more than the tolerance off it (a
/// display that will not lock to 60 Hz keeps one bit a frame at its own rate and reports it,
/// PROTOCOL-v3 section 6); a change of period re-anchors t0 so n never jumps.
///
/// It also measures the refresh (`RefreshMeter` in the JS: 20 warm-up intervals, then the mean
/// of the last 120 within 0.5..1.5 × their median), counts dropped frames (an interval over 1.5
/// periods while flickering, the `frames` report), hands out each cycle start
/// (n mod cycleFrames == 0) with its flip time (PROTOCOL-v3 `cycle`: a cycle start whose frame
/// was dropped is reported at the time it was due when the next frame drawn is at most 2 frames
/// later, and not at all when later), the onset, and a `frames` report every 5 s while
/// flickering. Pure value type: the app's display link feeds it, the tests feed it numbers.
public struct FrameClock: Sendable {
    /// One cycle start to report: cycle index `n` (frame `n * cycleFrames`) and its flip time.
    public struct Cycle: Equatable, Sendable {
        public let n: Int
        /// Media time of that cycle's first frame: the drawn frame's, or the time it was due.
        public let mediaTime: Double
        public init(n: Int, mediaTime: Double) {
            self.n = n
            self.mediaTime = mediaTime
        }
    }

    /// The `frames` report (PROTOCOL-v3): measured refresh and the running dropped count.
    public struct FramesReport: Equatable, Sendable {
        public let refreshHz: Double
        public let dropped: Int
        public init(refreshHz: Double, dropped: Int) {
            self.refreshHz = refreshHz
            self.dropped = dropped
        }
    }

    /// What one frame means: its index (nil while still), and what to report on it.
    public struct Tick: Equatable, Sendable {
        /// Frame index since the first flickering frame; nil while still.
        public var frame: Int?
        /// Media time of the first flickering frame, on that frame only (the `onset`).
        public var onset: Double?
        /// Cycle starts due on this frame (usually none, one every cycle).
        public var cycles: [Cycle] = []
        /// This frame came over 1.5 periods after the previous one while flickering.
        public var droppedBefore = false
        /// The `frames` report, every `framesEvery` seconds while flickering.
        public var frames: FramesReport?
        public init(frame: Int? = nil, onset: Double? = nil, cycles: [Cycle] = [],
                    droppedBefore: Bool = false, frames: FramesReport? = nil) {
            self.frame = frame
            self.onset = onset
            self.cycles = cycles
            self.droppedBefore = droppedBefore
            self.frames = frames
        }
    }

    // Settings.
    public let nominalHz: Double
    public let toleranceHz: Double
    public let cycleFrames: Int
    /// A cycle start drawn at most this many frames late is still reported (at its due time).
    public let lateFrames: Int
    public let framesEvery: Double
    /// An interval over this many periods is a dropped frame.
    public static let dropFactor = 1.5

    // Refresh meter.
    public let meterWarmup: Int
    public let meterSize: Int
    private var meterBuffer: [Double]
    private var meterSkipped = 0
    private var meterTotal = 0
    /// The measured refresh (Hz), nil until `meterWarmup + meterSize` intervals were seen.
    public private(set) var measuredHz: Double?

    // Counting.
    public private(set) var isRunning = false
    /// Dropped frames while flickering since this clock was made (the report's running total).
    public private(set) var dropped = 0
    /// Cycle starts drawn too late to report (not sent).
    public private(set) var missedCycles = 0
    /// The last frame index handed out (nil while still).
    public private(set) var frame: Int?
    private var t0: Double?
    private var countPeriod: Double
    private var lastT: Double?
    private var lastCycle = -1
    private var lastFramesAt: Double?
    private var startPending = false

    // Media time -> Unix time.
    private var unixOffset: Double?

    public init(nominalHz: Double = 60, toleranceHz: Double = 1, cycleFrames: Int = CodeVep.cycleFrames,
                lateFrames: Int = 2, framesEvery: Double = 5, meterWarmup: Int = 20, meterSize: Int = 120) {
        precondition(nominalHz > 0 && cycleFrames > 0 && meterSize > 0)
        self.nominalHz = nominalHz
        self.toleranceHz = toleranceHz
        self.cycleFrames = cycleFrames
        self.lateFrames = lateFrames
        self.framesEvery = framesEvery
        self.meterWarmup = meterWarmup
        self.meterSize = meterSize
        self.meterBuffer = Array(repeating: 0, count: meterSize)
        self.countPeriod = 1 / nominalHz
    }

    /// The rate frames are counted at: the nominal refresh, or the measured one when that is
    /// further than the tolerance from it.
    public var countingHz: Double {
        if let m = measuredHz, abs(m - nominalHz) > toleranceHz { return m }
        return nominalHz
    }

    /// The frame period (s) frames are counted at now.
    public var period: Double { 1 / countingHz }

    /// Flicker on: the next `tick` is frame 0 (the onset). Restarts the count when running.
    public mutating func start() {
        isRunning = true
        startPending = true
        t0 = nil
        frame = nil
        lastCycle = -1
    }

    /// Flicker off (squares drawn still). The next `start` counts from 0 again.
    public mutating func stop() {
        isRunning = false
        startPending = false
        t0 = nil
        frame = nil
        lastCycle = -1
        lastFramesAt = nil
    }

    /// Advance to the frame shown at media time `t` (the display link's `targetTimestamp`).
    /// Always feeds the refresh meter; while running, also counts. A repeated or earlier
    /// timestamp changes nothing and returns the current frame with nothing to report.
    @discardableResult
    public mutating func tick(_ t: Double) -> Tick {
        guard t.isFinite else { return Tick(frame: frame) }
        if let last = lastT, t <= last { return Tick(frame: frame) }
        var out = Tick()
        if let last = lastT {
            let interval = t - last
            pushInterval(interval)
            if isRunning, t0 != nil, interval > FrameClock.dropFactor * countPeriod {
                dropped += 1
                out.droppedBefore = true
            }
        }
        lastT = t
        guard isRunning else { return out }

        if startPending || t0 == nil {
            startPending = false
            t0 = t
            countPeriod = period
            lastCycle = -1
            lastFramesAt = t
            out.onset = t
        } else if countPeriod != period, let start = t0 {
            // A new refresh estimate changed the counting rate: this frame keeps the index the
            // old rate gives it, and later frames count on from it at the new rate.
            let kept = max(0, ((t - start) / countPeriod).rounded())
            countPeriod = period
            t0 = t - kept * countPeriod
        }
        let start = t0!
        let n = max(0, Int(((t - start) / countPeriod).rounded()))
        frame = n
        out.frame = n

        let c = n / cycleFrames
        if c > lastCycle {
            for k in (lastCycle + 1)...c {
                let late = n - k * cycleFrames
                if late > lateFrames { missedCycles += 1; continue }
                let due = late == 0 ? t : start + Double(k * cycleFrames) * countPeriod
                out.cycles.append(Cycle(n: k, mediaTime: due))
            }
            lastCycle = c
        }

        if let at = lastFramesAt, t - at >= framesEvery {
            lastFramesAt = t
            let hz = ((measuredHz ?? nominalHz) * 1000).rounded() / 1000
            out.frames = FramesReport(refreshHz: hz, dropped: dropped)
        }
        return out
    }

    // MARK: - Refresh meter

    private mutating func pushInterval(_ interval: Double) {
        guard interval > 0, interval.isFinite else { return }
        if meterSkipped < meterWarmup { meterSkipped += 1; return }
        meterBuffer[meterTotal % meterSize] = interval
        meterTotal += 1
        if meterTotal >= meterSize { measuredHz = FrameClock.meanRate(meterBuffer) }
    }

    /// 1 / the mean of the intervals within 0.5..1.5 × their median.
    static func meanRate(_ intervals: [Double]) -> Double? {
        guard !intervals.isEmpty else { return nil }
        let sorted = intervals.sorted()
        let n = sorted.count
        let median = n % 2 == 1 ? sorted[n / 2] : 0.5 * (sorted[n / 2 - 1] + sorted[n / 2])
        var sum = 0.0, count = 0
        for v in intervals where v > 0.5 * median && v < 1.5 * median {
            sum += v
            count += 1
        }
        return count > 0 && sum > 0 ? Double(count) / sum : nil
    }

    /// Forget the refresh estimate (a new screen or a long pause); the warm-up starts again.
    public mutating func resetMeter() {
        meterSkipped = 0
        meterTotal = 0
        measuredHz = nil
        lastT = nil
    }

    // MARK: - Unix time

    /// One simultaneous sample of the two clocks: `media` from `CACurrentMediaTime()` and `unix`
    /// from `Date().timeIntervalSince1970`. Flip times are media time; the server wants Unix.
    public mutating func syncClock(media: Double, unix: Double) {
        unixOffset = unix - media
    }

    /// True once `syncClock` was called.
    public var hasClockSync: Bool { unixOffset != nil }

    /// The Unix time (s) of media time `media`; without a `syncClock` sample, `media` itself.
    public func unixTime(media: Double) -> Double {
        media + (unixOffset ?? 0)
    }
}
