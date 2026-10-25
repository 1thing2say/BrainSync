import Foundation

// The c-VEP code the phone flickers (INTERFACE-v2-draft.md section 9), as pure frame arithmetic so
// it is tested without a screen. It is the Python side's rule exactly (brainsync.vep.codes
// .lagged_codes: target k = np.roll(cycle, lag_k)): slot s is bright on trial frame n when
// bits[((n - lag_s) mod cycleFrames) / framesPerBit] is "1". The bridge decodes against that same
// matrix, so one frame off here is a wrong template; Fixtures/cvep_stimulus_frames.json (tools/cvep_fixture.py)
// pins it to the Python output frame by frame.
//
// Frame indices come from the display link's timestamps rounded to the frame period (as
// ui/flicker_core.js does), never from counting callbacks, so a dropped frame skips one frame of
// the code instead of shifting every frame after it.

public struct CVEPCode: Equatable {
    /// Why a `stimulus` cannot be drawn.
    public enum Problem: Error, Equatable {
        case paradigm(String)
        case emptyBits
        case badBits(String)
        case framesPerBit(Int)
        case noLags
        case lag(slot: String, lag: Int)
        case depth(Double)
        case refreshHz(Double)
    }

    /// One period of the code, true = bright ("1").
    public let bits: [Bool]
    public let framesPerBit: Int
    /// Frames each slot runs behind slot lag 0, in 0..<cycleFrames.
    public let lags: [String: Int]
    /// The white layer's opacity on a bright frame (0 < depth <= 1).
    public let depth: Double
    /// The rate the code's frames assume (60).
    public let refreshHz: Double
    /// Frames in one cycle: bits.count * framesPerBit (63 for cvep_2).
    public let cycleFrames: Int
    /// The bit of every frame of one cycle (bits held framesPerBit frames), for the per-frame lookup.
    private let frameBits: [Bool]

    /// The code of a server `stimulus`; throws `Problem` when it cannot be drawn.
    public init(spec: StimulusSpec) throws {
        guard spec.paradigm == "code" else { throw Problem.paradigm(spec.paradigm) }
        try self.init(bits: spec.bits, framesPerBit: spec.framesPerBit, lags: spec.lags,
                      depth: spec.depth, refreshHz: spec.refreshHz)
    }

    public init(bits: String, framesPerBit: Int, lags: [String: Int], depth: Double = 1,
                refreshHz: Double = 60) throws {
        guard !bits.isEmpty else { throw Problem.emptyBits }
        var parsed: [Bool] = []
        parsed.reserveCapacity(bits.count)
        for ch in bits {
            switch ch {
            case "1": parsed.append(true)
            case "0": parsed.append(false)
            default: throw Problem.badBits(bits)
            }
        }
        guard framesPerBit >= 1 else { throw Problem.framesPerBit(framesPerBit) }
        guard !lags.isEmpty else { throw Problem.noLags }
        let cycle = parsed.count * framesPerBit
        for (slot, lag) in lags where lag < 0 || lag >= cycle {
            throw Problem.lag(slot: slot, lag: lag)
        }
        guard depth.isFinite, depth > 0, depth <= 1 else { throw Problem.depth(depth) }
        guard refreshHz.isFinite, refreshHz > 0 else { throw Problem.refreshHz(refreshHz) }
        self.bits = parsed
        self.framesPerBit = framesPerBit
        self.lags = lags
        self.depth = depth
        self.refreshHz = refreshHz
        self.cycleFrames = cycle
        self.frameBits = parsed.flatMap { Array(repeating: $0, count: framesPerBit) }
    }

    /// `a mod n` in 0..<n, also for negative `a` (Swift's % keeps the sign).
    @inlinable
    public static func mod(_ a: Int, _ n: Int) -> Int {
        let r = a % n
        return r < 0 ? r + n : r
    }

    /// Whether a target `lag` frames behind is bright on trial frame `frame` (any integer).
    public func isBright(lag: Int, frame: Int) -> Bool {
        frameBits[Self.mod(frame - lag, cycleFrames)]
    }

    /// Whether `slot` is bright on trial frame `frame`; false for a slot the code does not have.
    public func isBright(slot: String, frame: Int) -> Bool {
        guard let lag = lags[slot] else { return false }
        return isBright(lag: lag, frame: frame)
    }

    /// The white layer's opacity for `slot` on `frame`: `depth` when bright, 0 when dark or unknown.
    public func opacity(slot: String, frame: Int) -> Double {
        isBright(slot: slot, frame: frame) ? depth : 0
    }

    /// The opacity of a target `lag` frames behind (the engine's per-frame path, no dictionary).
    public func opacity(lag: Int, frame: Int) -> Double {
        isBright(lag: lag, frame: frame) ? depth : 0
    }

    /// True on the first frame of a cycle (0, 63, 126, ... for cvep_2): a `cycle_markers` frame.
    public func isCycleStart(frame: Int) -> Bool {
        frame >= 0 && frame % cycleFrames == 0
    }

    /// The cycle a frame belongs to (floor division: frame -1 is in cycle -1).
    public func cycle(ofFrame frame: Int) -> Int {
        (frame - Self.mod(frame, cycleFrames)) / cycleFrames
    }

    /// The first frame of `cycle`.
    public func firstFrame(ofCycle cycle: Int) -> Int {
        cycle * cycleFrames
    }

    /// The trial frame shown `elapsed` seconds after the onset frame (both display-link target
    /// timestamps), rounded to the frame period; never negative, 0 for a bad period.
    public static func frameIndex(elapsed: Double, framePeriod: Double) -> Int {
        guard framePeriod > 0, framePeriod.isFinite, elapsed.isFinite, elapsed > 0 else { return 0 }
        let n = (elapsed / framePeriod).rounded()
        return n >= Double(Int.max) ? Int.max : Int(n)
    }
}
