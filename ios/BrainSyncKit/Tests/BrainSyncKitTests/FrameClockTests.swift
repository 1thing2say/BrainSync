import XCTest
@testable import BrainSyncKit

final class FrameClockTests: XCTestCase {
    private let p = 1.0 / 60.0

    /// Feeds `count` frames at 60 Hz from `base`, skipping `skip`, with optional jitter.
    private func run(_ clock: inout FrameClock, base: Double = 100, count: Int, skip: Set<Int> = [],
                     jitter: [Double] = [0]) -> [(k: Int, tick: FrameClock.Tick)] {
        var out: [(Int, FrameClock.Tick)] = []
        for k in 0..<count where !skip.contains(k) {
            out.append((k, clock.tick(base + Double(k) * p + jitter[k % jitter.count])))
        }
        return out
    }

    // MARK: - Against FlickerState (clients/js/brainsync-client.js)

    func testMatchesFlickerStateSequences() throws {
        let rows = try XCTUnwrap(try cvepFixture()["clock"] as? [[String: Any]])
        XCTAssertEqual(rows.count, 3)
        for row in rows {
            let name = row["name"] as? String ?? "?"
            let stamps = try XCTUnwrap(row["stamps_ms"] as? [Double])
            let ticks = try XCTUnwrap(row["ticks"] as? [[String: Any]])
            XCTAssertEqual(stamps.count, ticks.count)
            var clock = FrameClock()
            clock.start()
            for (t, expected) in zip(stamps, ticks) {
                let tick = clock.tick(t / 1000)
                XCTAssertEqual(tick.frame, expected["frame"] as? Int, "\(name) at \(t)")
                if let onset = expected["onset"] as? Double {
                    XCTAssertEqual(try XCTUnwrap(tick.onset), onset, accuracy: 1e-9, name)
                } else {
                    XCTAssertNil(tick.onset, name)
                }
                let cycles = try XCTUnwrap(expected["cycles"] as? [[Double]])
                XCTAssertEqual(tick.cycles.map(\.n), cycles.map { Int($0[0]) }, "\(name) at \(t)")
                for (got, want) in zip(tick.cycles, cycles) {
                    XCTAssertEqual(got.mediaTime, want[1], accuracy: 1e-9, "\(name) cycle \(got.n)")
                }
                XCTAssertEqual(clock.dropped, expected["dropped"] as? Int, "\(name) at \(t)")
            }
            XCTAssertEqual(clock.missedCycles, row["missed_cycles"] as? Int, name)
        }
    }

    // MARK: - Counting

    func testSteadyFramesCountFromZeroWithOnset() {
        var clock = FrameClock()
        XCTAssertNil(clock.tick(99.9).frame)            // still: no frame index
        clock.start()
        let ticks = run(&clock, count: 130)
        XCTAssertEqual(ticks.map { $0.tick.frame }, (0..<130).map { $0 })
        XCTAssertEqual(ticks[0].tick.onset, 100)
        XCTAssertNil(ticks[1].tick.onset)
        let cycles = ticks.flatMap(\.tick.cycles)
        XCTAssertEqual(cycles.map(\.n), [0, 1, 2])
        XCTAssertEqual(cycles[2].mediaTime, 100 + 126 * p, accuracy: 1e-9)
        XCTAssertEqual(clock.dropped, 0)
    }

    func testJitterNeverShiftsTheCode() {
        // Up to ±2 ms off the vsync grid (a display link stays on it): still one bit a frame.
        var clock = FrameClock()
        clock.start()
        let ticks = run(&clock, count: 300, jitter: [0, 0.002, -0.002, 0.001, -0.0015])
        XCTAssertEqual(ticks.map { $0.tick.frame }, (0..<300).map { $0 })
        XCTAssertEqual(clock.dropped, 0)
        XCTAssertEqual(ticks.flatMap(\.tick.cycles).map(\.n), [0, 1, 2, 3, 4])
    }

    func testDroppedFrameSkipsItsBitsAndIsCounted() {
        var clock = FrameClock()
        clock.start()
        let ticks = run(&clock, count: 40, skip: [10, 20, 21])
        let frames = ticks.map { $0.tick.frame! }
        XCTAssertEqual(frames, ticks.map(\.k))          // the frame after a hole is back on the code
        XCTAssertEqual(clock.dropped, 2)                // one per late interval, not per frame
        XCTAssertTrue(ticks.first { $0.k == 11 }!.tick.droppedBefore)
        XCTAssertFalse(ticks.first { $0.k == 12 }!.tick.droppedBefore)
    }

    // MARK: - The cycle rule (PROTOCOL-v3 section 6)

    func testCycleStartDroppedReportedAtDueTimeUpToTwoFramesLate() {
        for late in [1, 2] {
            var clock = FrameClock()
            clock.start()
            let skip = Set(63..<(63 + late))
            let ticks = run(&clock, count: 70, skip: skip)
            let at = ticks.first { $0.k == 63 + late }!.tick
            XCTAssertEqual(at.cycles.map(\.n), [1], "late \(late)")
            XCTAssertEqual(at.cycles[0].mediaTime, 100 + 63 * p, accuracy: 1e-9)   // when it was due
            XCTAssertEqual(clock.missedCycles, 0)
        }
    }

    func testCycleStartMissedByMoreIsNotSent() {
        var clock = FrameClock()
        clock.start()
        let ticks = run(&clock, count: 140, skip: [63, 64, 65])
        XCTAssertEqual(ticks.flatMap(\.tick.cycles).map(\.n), [0, 2])
        XCTAssertEqual(clock.missedCycles, 1)
    }

    func testOnTimeCycleUsesTheDrawnFramesTime() {
        var clock = FrameClock()
        clock.start()
        let ticks = run(&clock, count: 64, jitter: [0.002])
        let c = ticks[63].tick.cycles[0]
        XCTAssertEqual(c.mediaTime, 100 + 63 * p + 0.002, accuracy: 1e-12)
    }

    // MARK: - Start, stop, frames report

    func testStopThenStartCountsFromZeroAgain() {
        var clock = FrameClock()
        clock.start()
        _ = run(&clock, count: 70)
        clock.stop()
        XCTAssertFalse(clock.isRunning)
        XCTAssertNil(clock.tick(100 + 70 * p).frame)
        clock.start()
        let t = clock.tick(100 + 71 * p)
        XCTAssertEqual(t.frame, 0)
        XCTAssertEqual(t.onset, 100 + 71 * p)
        XCTAssertEqual(t.cycles.map(\.n), [0])
        // A long still gap is not a dropped frame.
        clock.stop()
        clock.start()
        XCTAssertFalse(clock.tick(200).droppedBefore)
        XCTAssertEqual(clock.dropped, 0)
    }

    func testRepeatedOrEarlierTimestampIsANoOp() {
        var clock = FrameClock()
        clock.start()
        _ = clock.tick(100)
        _ = clock.tick(100 + p)
        let again = clock.tick(100 + p)
        XCTAssertEqual(again.frame, 1)
        XCTAssertTrue(again.cycles.isEmpty)
        XCTAssertEqual(clock.tick(50).frame, 1)
    }

    func testFramesReportEveryFiveSeconds() {
        var clock = FrameClock()
        clock.start()
        let reports = run(&clock, count: 60 * 11 + 1, skip: [200]).compactMap(\.tick.frames)
        XCTAssertEqual(reports.count, 2)
        XCTAssertEqual(reports[0].dropped, 1)
        XCTAssertEqual(reports[1].refreshHz, 60, accuracy: 0.01)
    }

    // MARK: - Refresh estimate

    func testRefreshMeterWarmsUpThenMeasures() {
        var clock = FrameClock()
        _ = run(&clock, count: 140)                     // 139 intervals: warm-up 20 + 119
        XCTAssertNil(clock.measuredHz)
        _ = clock.tick(100 + 140 * p)
        XCTAssertEqual(try XCTUnwrap(clock.measuredHz), 60, accuracy: 1e-6)
    }

    func testRefreshMeterIgnoresDroppedFrames() {
        var clock = FrameClock()
        _ = run(&clock, count: 400, skip: Set(stride(from: 30, to: 400, by: 17)))
        XCTAssertEqual(try XCTUnwrap(clock.measuredHz), 60, accuracy: 0.01)
    }

    func testMeasuredOtherRateCountsAtItsOwnRateWithoutJumping() {
        // A display stuck at 120 Hz keeps one bit a frame at 120 Hz once measured; the switch
        // from the nominal 60 Hz count re-anchors so the index keeps rising by one a frame.
        var clock = FrameClock()
        clock.start()
        let q = 1.0 / 120
        var frames: [Int] = []
        for k in 0..<300 { frames.append(clock.tick(100 + Double(k) * q).frame!) }
        XCTAssertEqual(try XCTUnwrap(clock.measuredHz), 120, accuracy: 1e-6)
        XCTAssertEqual(clock.countingHz, 120, accuracy: 1e-6)
        let after = Array(frames.suffix(100))
        XCTAssertEqual(zip(after, after.dropFirst()).filter { $1 - $0 != 1 }.count, 0)
        XCTAssertTrue(zip(frames, frames.dropFirst()).allSatisfy { $1 >= $0 })
    }

    func testRateWithinToleranceKeepsNominalCount() {
        var clock = FrameClock()
        clock.start()
        for k in 0..<200 { _ = clock.tick(100 + Double(k) / 59.94) }
        XCTAssertEqual(try XCTUnwrap(clock.measuredHz), 59.94, accuracy: 1e-6)
        XCTAssertEqual(clock.countingHz, 60)
    }

    // MARK: - Unix time

    func testMediaToUnix() {
        var clock = FrameClock()
        XCTAssertFalse(clock.hasClockSync)
        XCTAssertEqual(clock.unixTime(media: 5), 5)
        clock.syncClock(media: 1000, unix: 1_792_809_200)
        XCTAssertTrue(clock.hasClockSync)
        XCTAssertEqual(clock.unixTime(media: 1001.5), 1_792_809_201.5, accuracy: 1e-6)
    }
}
