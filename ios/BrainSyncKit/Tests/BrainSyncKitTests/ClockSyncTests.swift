import XCTest
@testable import BrainSyncKit

/// ClockSync: offset = t_server - (t_send + t_receive) / 2 of the shortest round trip among the last
/// `window` pings (INTERFACE-v2-draft.md section 9).
final class ClockSyncTests: XCTestCase {
    func testNoOffsetBeforeTheFirstPong() {
        let sync = ClockSync()
        XCTAssertNil(sync.best)
        XCTAssertNil(sync.offset)
        XCTAssertNil(sync.serverTime(100))
    }

    func testOffsetMath() throws {
        var sync = ClockSync()
        // Sent at 100.0, answered at server time 150.05, back at 100.1: the midpoint is 100.05.
        sync.add(tSend: 100.0, tReceive: 100.1, tServer: 150.05)
        let best = try XCTUnwrap(sync.best)
        XCTAssertEqual(best.offset, 50.0, accuracy: 1e-9)
        XCTAssertEqual(best.roundTrip, 0.1, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(sync.serverTime(200)), 250.0, accuracy: 1e-9)
    }

    func testTheShortestRoundTripWins() throws {
        var sync = ClockSync()
        sync.add(tSend: 0, tReceive: 0.200, tServer: 10.150)   // offset 10.05, slow
        sync.add(tSend: 1, tReceive: 1.010, tServer: 11.006)   // offset 10.001, fast
        sync.add(tSend: 2, tReceive: 2.080, tServer: 12.000)   // offset 9.96
        XCTAssertEqual(try XCTUnwrap(sync.offset), 10.001, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(sync.best).roundTrip, 0.010, accuracy: 1e-9)
    }

    func testOnlyTheLastWindowCounts() throws {
        var sync = ClockSync(window: 3)
        sync.add(tSend: 0, tReceive: 0.001, tServer: 5.0005)   // best ever, then pushed out
        for i in 1...3 {
            let t = Double(i)
            sync.add(tSend: t, tReceive: t + 0.05, tServer: t + 7.025)
        }
        XCTAssertEqual(sync.samples.count, 3)
        XCTAssertEqual(try XCTUnwrap(sync.offset), 7.0, accuracy: 1e-9)
        XCTAssertEqual(ClockSync(window: 0).window, 1)
    }

    func testNegativeRoundTripAndNonFiniteTimesAreIgnored() {
        var sync = ClockSync()
        sync.add(tSend: 10, tReceive: 9.9, tServer: 20)
        sync.add(tSend: .nan, tReceive: 10, tServer: 20)
        sync.add(tSend: 10, tReceive: 10.1, tServer: .infinity)
        XCTAssertTrue(sync.samples.isEmpty)
        XCTAssertNil(sync.offset)
        sync.add(tSend: 10, tReceive: 10, tServer: 20)  // a zero round trip is fine
        XCTAssertEqual(sync.offset, 10)
    }

    func testReset() {
        var sync = ClockSync()
        sync.add(tSend: 0, tReceive: 0.1, tServer: 5)
        sync.reset()
        XCTAssertNil(sync.offset)
    }
}
