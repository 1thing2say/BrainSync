import Foundation

/// Rolling display-refresh estimate, a port of `RefreshMeter` in clients/js/brainsync-client.js:
/// skips `warmup` intervals, then keeps the last `size` in a ring. `hz` is 1 / the mean of the
/// intervals within 0.5..1.5 times their median, once `size` were seen. The mean, not the
/// median, so coarse timestamps do not bias the estimate.
public struct RefreshMeter: Sendable {
    public static let warmupFrames = 20
    public static let refreshFrames = 120

    public let size: Int
    public let warmup: Int
    private var buf: [Double]
    private var skipped = 0
    public private(set) var total = 0

    public init(size: Int = RefreshMeter.refreshFrames, warmup: Int = RefreshMeter.warmupFrames) {
        self.size = max(1, size)
        self.warmup = max(0, warmup)
        buf = Array(repeating: 0, count: self.size)
    }

    /// Add one frame interval (any unit, seconds by convention); false when it was skipped
    /// (warm-up, not positive, not finite).
    @discardableResult
    public mutating func push(_ interval: Double) -> Bool {
        guard interval > 0, interval.isFinite else { return false }
        if skipped < warmup { skipped += 1; return false }
        buf[total % size] = interval
        total += 1
        return true
    }

    public var ready: Bool { total >= size }

    /// True on the pushes where a fresh estimate is due (every `size` intervals).
    public var due: Bool { ready && total % size == 0 }

    /// The refresh in Hz for intervals in seconds (`hz(unit: 1000)` for milliseconds), or nil
    /// before `size` intervals were seen.
    public func hz(unit: Double = 1.0) -> Double? {
        guard ready else { return nil }
        let med = RefreshMeter.median(buf)
        var sum = 0.0, n = 0
        for v in buf where v > 0.5 * med && v < 1.5 * med { sum += v; n += 1 }
        return n > 0 ? unit / (sum / Double(n)) : nil
    }

    public static func median(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return .nan }
        let s = values.sorted()
        let n = s.count
        return n % 2 == 1 ? s[(n - 1) / 2] : 0.5 * (s[n / 2 - 1] + s[n / 2])
    }
}
