import XCTest
@testable import BrainSyncKit

final class SetupStepsTests: XCTestCase {
    func testFullOrderWhenNotPaired() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.steps, [.welcome, .pair, .safety, .microphone, .practice, .done])
        XCTAssertEqual(s.first, .welcome)
        XCTAssertEqual(s.count, 6)
    }

    func testPairedSkipsPair() {
        let s = SetupSteps(paired: true)
        XCTAssertEqual(s.steps, [.welcome, .safety, .microphone, .practice, .done])
        XCTAssertEqual(s.count, 5)
        XCTAssertEqual(s.next(after: .welcome), .safety)
        XCTAssertEqual(s.back(before: .safety), .welcome)
        // A skipped step has no place in the order.
        XCTAssertNil(s.next(after: .pair))
        XCTAssertNil(s.back(before: .pair))
        XCTAssertNil(s.progress(of: .pair))
    }

    func testNextAndBack() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.next(after: .welcome), .pair)
        XCTAssertEqual(s.next(after: .pair), .safety)
        XCTAssertEqual(s.next(after: .practice), .done)
        XCTAssertNil(s.next(after: .done))
        XCTAssertEqual(s.back(before: .done), .practice)
        XCTAssertEqual(s.back(before: .pair), .welcome)
        XCTAssertNil(s.back(before: .welcome))
    }

    func testProgress() {
        let s = SetupSteps(paired: false)
        XCTAssertEqual(s.progress(of: .welcome), SetupSteps.Progress(index: 0, count: 6))
        XCTAssertEqual(s.progress(of: .safety), SetupSteps.Progress(index: 2, count: 6))
        XCTAssertEqual(s.progress(of: .done), SetupSteps.Progress(index: 5, count: 6))
        let paired = SetupSteps(paired: true)
        XCTAssertEqual(paired.progress(of: .safety), SetupSteps.Progress(index: 1, count: 5))
        XCTAssertEqual(paired.progress(of: .done), SetupSteps.Progress(index: 4, count: 5))
    }

    func testWalkVisitsEveryStepOnceAndBackAgain() {
        for paired in [false, true] {
            let s = SetupSteps(paired: paired)
            var forward: [SetupStep] = [s.first]
            while let next = s.next(after: forward.last!) { forward.append(next) }
            XCTAssertEqual(forward, s.steps)
            XCTAssertEqual(forward.last, .done)
            var backward: [SetupStep] = [.done]
            while let previous = s.back(before: backward.last!) { backward.append(previous) }
            XCTAssertEqual(backward, s.steps.reversed())
        }
    }

    func testSafetyAlwaysComesBeforeDone() {
        // The warning is read before the setup's last step starts the session (PLAN rule 10).
        for paired in [false, true] {
            let steps = SetupSteps(paired: paired).steps
            XCTAssertLessThan(steps.firstIndex(of: .safety)!, steps.firstIndex(of: .done)!)
            XCTAssertEqual(steps.last, .done)
        }
    }
}
