import XCTest
@testable import BrainSyncKit

/// The refresh check: 60 Hz steady passes; 120 Hz (ProMotion not capped), 30 Hz and uneven frames
/// fail with a reason; dropped frames are counted.
final class FrameRateCheckTests: XCTestCase {
    private func feed(_ check: inout FrameRateCheck, hz: Double, frames: Int, start: Double = 1000,
                      jitter: (Int) -> Double = { _ in 0 }) -> Double {
        var t = start
        for i in 0..<frames {
            check.add(t + jitter(i))
            t += 1 / hz
        }
        return t
    }

    func testNoVerdictBeforeASecondOfFrames() {
        var check = FrameRateCheck()
        _ = feed(&check, hz: 60, frames: 30)
        XCTAssertNil(check.measuredHz)
        XCTAssertFalse(check.ok)
        XCTAssertEqual(check.message, "measuring the refresh rate")
        XCTAssertNil(check.evaluate())
    }

    func testSteady60HzIsOk() throws {
        var check = FrameRateCheck()
        _ = feed(&check, hz: 60, frames: 200)
        let v = try XCTUnwrap(check.evaluate())
        XCTAssertEqual(v.measuredHz, 60, accuracy: 0.01)
        XCTAssertEqual(v.framePeriod, 1.0 / 60, accuracy: 1e-9)
        XCTAssertTrue(v.ok)
        XCTAssertEqual(v.message, "")
        XCTAssertEqual(check.droppedFrames, 0)
        XCTAssertEqual(check.intervalCount, 120)
    }

    func testSlightlyOff60WithSmallJitterIsOk() {
        var check = FrameRateCheck()
        _ = feed(&check, hz: 59.94, frames: 200) { i in (i % 2 == 0 ? 1 : -1) * 0.0005 }
        XCTAssertTrue(check.ok)
        XCTAssertEqual(check.measuredHz ?? 0, 59.94, accuracy: 0.2)
    }

    func test120HzIsNotOk() {
        var check = FrameRateCheck()
        _ = feed(&check, hz: 120, frames: 300)
        XCTAssertFalse(check.ok)
        XCTAssertEqual(check.measuredHz ?? 0, 120, accuracy: 0.01)
        XCTAssertEqual(check.message, "120 Hz: ProMotion is not capped at 60 Hz")
        XCTAssertEqual(check.droppedFrames, 0)
    }

    func test30HzIsNotOk() {
        var check = FrameRateCheck()
        _ = feed(&check, hz: 30, frames: 200)
        XCTAssertFalse(check.ok)
        XCTAssertTrue(check.message.hasPrefix("30 Hz, needs 60 Hz"), check.message)
    }

    func testUnevenFramesAreNotOk() {
        var check = FrameRateCheck()
        // A variable refresh: every 10th interval twice as long (10 % uneven).
        var t = 1000.0
        for i in 0..<200 {
            check.add(t)
            t += i % 10 == 0 ? 2.0 / 60 : 1.0 / 60
        }
        XCTAssertFalse(check.ok)
        XCTAssertEqual(check.message, "uneven frames: close other apps")
        XCTAssertEqual(check.measuredHz ?? 0, 60, accuracy: 0.01)
    }

    func testOneStallInTwoSecondsIsStillOkButThreeAreNot() {
        var check = FrameRateCheck()
        var t = 1000.0
        for i in 0..<121 {  // 120 intervals, one of them a 4-frame stall
            check.add(t)
            t += i == 50 ? 4.0 / 60 : 1.0 / 60
        }
        XCTAssertTrue(check.ok, check.message)
        XCTAssertEqual(check.droppedFrames, 3)

        var bad = FrameRateCheck()
        t = 1000.0
        for i in 0..<121 {
            bad.add(t)
            t += [20, 60, 100].contains(i) ? 2.0 / 60 : 1.0 / 60
        }
        XCTAssertFalse(bad.ok)  // 3 of 120 = 2.5 %
        XCTAssertEqual(bad.droppedFrames, 3)
    }

    func testAPauseIsNotAFrameAndResetForgets() {
        var check = FrameRateCheck()
        let t = feed(&check, hz: 60, frames: 100)
        _ = feed(&check, hz: 60, frames: 100, start: t + 5)  // 5 s in the background
        XCTAssertTrue(check.ok)
        XCTAssertEqual(check.droppedFrames, 0)
        check.reset()
        XCTAssertNil(check.measuredHz)
        XCTAssertEqual(check.frameCount, 0)
    }

    func testDroppedFramesFromIndexJumps() {
        XCTAssertEqual(FrameRateCheck.droppedFrames(from: 4, to: 5), 0)
        XCTAssertEqual(FrameRateCheck.droppedFrames(from: 4, to: 7), 2)
        XCTAssertEqual(FrameRateCheck.droppedFrames(from: 4, to: 4), 0)
        XCTAssertEqual(FrameRateCheck.droppedFrames(from: -1, to: 0), 0)
    }
}
