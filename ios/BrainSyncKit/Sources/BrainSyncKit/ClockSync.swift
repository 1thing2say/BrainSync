import Foundation

// The phone's estimate of the server's clock (INTERFACE-v2-draft.md section 9). The bridge cuts its
// c-VEP epochs at cycle onsets on the Mac's clock, so the phone sends its frame times converted
// with this offset. Each clock_ping / clock_pong gives one sample: the server answered somewhere
// between the send and the receive, so offset = t_server - (t_send + t_receive) / 2 is off by at
// most half the round trip. The sample with the shortest round trip among the last `window` wins.
// A constant error does not matter (the template is trained through the same path); jitter does.

public struct ClockSync: Equatable {
    public struct Sample: Equatable {
        public var offset: Double
        public var roundTrip: Double
    }

    public let window: Int
    public private(set) var samples: [Sample] = []

    public init(window: Int = 20) {
        self.window = max(1, window)
    }

    /// One ping: sent at `tSend` and answered (`tServer`) by `tReceive`, phone times in Unix
    /// seconds. A pong that came back before it was sent is ignored.
    public mutating func add(tSend: Double, tReceive: Double, tServer: Double) {
        let roundTrip = tReceive - tSend
        guard roundTrip >= 0, tSend.isFinite, tReceive.isFinite, tServer.isFinite else { return }
        samples.append(Sample(offset: tServer - (tSend + tReceive) / 2, roundTrip: roundTrip))
        if samples.count > window { samples.removeFirst(samples.count - window) }
    }

    /// The best sample: the shortest round trip in the window; nil before the first pong.
    public var best: Sample? { samples.min { $0.roundTrip < $1.roundTrip } }

    /// Seconds to add to a phone time to get the server's; nil before the first pong.
    public var offset: Double? { best?.offset }

    /// The phone time `t` on the server's clock; nil before the first pong.
    public func serverTime(_ t: Double) -> Double? {
        offset.map { t + $0 }
    }

    public mutating func reset() {
        samples.removeAll()
    }
}
